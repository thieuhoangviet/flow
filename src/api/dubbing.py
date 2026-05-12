"""Video dubbing API: STT → Translate → TTS → Replace voice."""
import asyncio
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_whisper_model = None  # Cache model instance


def _get_ffmpeg_path() -> str:
    """Get ffmpeg binary path from imageio-ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _get_whisper_model():
    """Lazy-load faster-whisper model (cached)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        print("[DUBBING] Whisper model loaded (base, cpu, int8)")
    return _whisper_model


def _transcribe_audio_sync(ffmpeg: str, video_path: str) -> dict:
    """Extract audio and transcribe speech using faster-whisper (blocking).
    Returns detected speech segments with text, timestamps, and speaker info.
    """
    try:
        audio_path = tempfile.mktemp(suffix=".wav", prefix="stt_")
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {"error": f"Audio extract failed: {result.stderr[-200:]}"}

        model = _get_whisper_model()
        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            language=None,  # Auto-detect language (was hardcoded "en" causing garbled results on non-English audio)
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300),
            word_timestamps=True,
        )

        speech_segments = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                speech_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "duration": round(seg.end - seg.start, 2),
                    "text": text,
                })

        try:
            Path(audio_path).unlink()
        except Exception:
            pass

        if not speech_segments:
            return {"segments": [], "language": info.language, "message": "No speech detected"}

        return {
            "segments": speech_segments,
            "language": info.language,
            "duration": round(info.duration, 2),
        }
    except Exception as e:
        return {"error": f"Transcription failed: {str(e)}"}


def _estimate_f0(samples: list, sample_rate: int = 16000) -> float:
    """Estimate fundamental frequency (F0) using autocorrelation method.
    
    This is much more accurate than zero-crossing rate for determining
    whether a voice is male or female.
    
    Returns F0 in Hz, or 0 if detection fails.
    Typical ranges: Male 85-180Hz, Female 165-255Hz.
    """
    n = len(samples)
    if n < sample_rate // 4:  # Need at least 250ms of audio
        return 0.0

    # Use a window of audio for analysis (up to 1 second)
    window_size = min(n, sample_rate)
    window = samples[:window_size]

    # Autocorrelation
    # Search for F0 between 60Hz and 400Hz
    min_lag = sample_rate // 400  # 400Hz upper bound
    max_lag = sample_rate // 60   # 60Hz lower bound
    max_lag = min(max_lag, window_size // 2)

    if min_lag >= max_lag:
        return 0.0

    # Compute normalized autocorrelation
    best_lag = 0
    best_corr = -1.0

    # Mean removal
    mean_val = sum(window) / len(window)
    centered = [s - mean_val for s in window]

    # Energy of the signal
    energy = sum(s * s for s in centered)
    if energy < 1e-6:
        return 0.0

    for lag in range(min_lag, max_lag):
        corr = 0.0
        for i in range(window_size - lag):
            corr += centered[i] * centered[i + lag]
        # Normalize
        corr /= energy
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    if best_lag <= 0 or best_corr < 0.2:  # Low confidence threshold
        return 0.0

    f0 = sample_rate / best_lag
    return round(f0, 1)


def _classify_gender(f0: float) -> str:
    """Classify voice gender based on fundamental frequency.
    
    Based on speech science:
    - Male: typically 85-165Hz (average ~120Hz)
    - Female: typically 165-300Hz (average ~210Hz)
    - Threshold at 165Hz
    """
    if f0 <= 0:
        return "unknown"
    if f0 < 165:
        return "male"
    return "female"


def _is_clear_dialogue(f0_values: list, genders: list) -> bool:
    """Determine if the video has a clear multi-speaker dialogue.
    
    A clear dialogue requires:
    1. At least 2 segments with valid F0
    2. F0 values must cluster into 2 clearly separated groups
    3. The gap between clusters must be significant (>40Hz)
    
    This prevents a single speaker from being split into male/female
    due to F0 fluctuation between AI-generated scenes.
    """
    valid = [(f0, g) for f0, g in zip(f0_values, genders) if f0 > 0]
    if len(valid) < 2:
        return False
    
    male_f0s = [f0 for f0, g in valid if g == "male"]
    female_f0s = [f0 for f0, g in valid if g == "female"]
    
    # Need both male and female segments
    if not male_f0s or not female_f0s:
        return False
    
    # Check if the clusters are well-separated
    avg_male = sum(male_f0s) / len(male_f0s)
    avg_female = sum(female_f0s) / len(female_f0s)
    gap = abs(avg_female - avg_male)
    
    # Also check variance within each cluster
    # If one "cluster" has high variance, it's probably noise
    if len(male_f0s) > 1:
        male_var = sum((f0 - avg_male) ** 2 for f0 in male_f0s) / len(male_f0s)
    else:
        male_var = 0
    if len(female_f0s) > 1:
        female_var = sum((f0 - avg_female) ** 2 for f0 in female_f0s) / len(female_f0s)
    else:
        female_var = 0
    
    max_std = max(male_var ** 0.5, female_var ** 0.5)
    
    # Clear dialogue: gap between clusters >> within-cluster variance
    # AND gap must be at least 40Hz
    is_clear = gap > 40 and gap > max_std * 2
    
    print(f"[DUBBING] Dialogue check: male_avg={avg_male:.0f}Hz ({len(male_f0s)} segs), "
          f"female_avg={avg_female:.0f}Hz ({len(female_f0s)} segs), "
          f"gap={gap:.0f}Hz, max_std={max_std:.0f}Hz → {'DIALOGUE' if is_clear else 'SINGLE SPEAKER'}")
    
    return is_clear


def _detect_speakers(ffmpeg: str, video_path: str, segments: list) -> list:
    """Analyze pitch of each segment to detect different speakers and their gender.
    
    Uses F0 (fundamental frequency) estimation via autocorrelation to:
    1. Determine the gender of each segment's speaker
    2. Cluster segments into consistent speakers
    3. Ensure the same speaker always gets the same voice
    
    IMPORTANT: For AI-generated videos with a single character, F0 can fluctuate
    significantly between scenes. We use global voting to force consistency
    when there's no clear multi-speaker dialogue pattern.
    
    Returns segments with 'speaker' and 'gender' fields added.
    """
    try:
        audio_path = tempfile.mktemp(suffix=".wav", prefix="pitch_")
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        f0_values = []
        for seg in segments:
            seg_audio = tempfile.mktemp(suffix=".wav", prefix="seg_")
            cmd = [
                ffmpeg, "-y", "-i", audio_path,
                "-ss", str(seg["start"]), "-t", str(seg["duration"]),
                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                seg_audio
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            # Read WAV samples and estimate F0
            f0 = 0.0
            try:
                with open(seg_audio, "rb") as f:
                    f.read(44)  # Skip WAV header
                    raw = f.read()
                n_samples = len(raw) // 2
                if n_samples > 400:  # Need minimum samples
                    samples = list(struct.unpack(f"<{n_samples}h", raw[:n_samples * 2]))
                    f0 = _estimate_f0(samples, 16000)
            except Exception:
                pass

            f0_values.append(f0)

            try:
                Path(seg_audio).unlink()
            except Exception:
                pass

        try:
            Path(audio_path).unlink()
        except Exception:
            pass

        # Classify each segment's gender based on F0
        genders = [_classify_gender(f0) for f0 in f0_values]

        # Log analysis results
        print(f"[DUBBING] F0 analysis results:")
        for i, (seg, f0, gender) in enumerate(zip(segments, f0_values, genders)):
            print(f"  [{seg['start']:.1f}s-{seg['end']:.1f}s] F0={f0:.0f}Hz → {gender}: \"{seg['text'][:50]}\"")

        # ================================================================
        # GLOBAL VOTING: Force consistency for single-speaker videos
        # ================================================================
        # AI-generated video scenes often have wildly different F0 values
        # for the SAME character. We must detect this and force consistency.
        
        # Count detected genders (excluding unknown)
        known_genders = [g for g in genders if g != "unknown"]
        male_count = sum(1 for g in known_genders if g == "male")
        female_count = sum(1 for g in known_genders if g == "female")
        
        # Compute median F0 for global gender decision
        valid_f0s = sorted([f0 for f0 in f0_values if f0 > 0])
        if valid_f0s:
            median_f0 = valid_f0s[len(valid_f0s) // 2]
        else:
            median_f0 = 0
        
        # Determine majority gender using both vote count AND median F0
        if male_count > 0 or female_count > 0:
            # Use weighted approach: both voting and median F0 should agree
            vote_gender = "male" if male_count >= female_count else "female"
            f0_gender = _classify_gender(median_f0) if median_f0 > 0 else vote_gender
            
            # If vote and F0 agree, use that; otherwise trust F0 median more
            if vote_gender == f0_gender:
                majority_gender = vote_gender
            else:
                # Conflict: trust median F0 (more robust than individual readings)
                majority_gender = f0_gender
                print(f"[DUBBING] Vote/F0 conflict: votes say {vote_gender} "
                      f"(M:{male_count}/F:{female_count}), median F0={median_f0:.0f}Hz "
                      f"says {f0_gender} → using {f0_gender}")
        else:
            majority_gender = "female"  # Default fallback
        
        # Check if this is a genuine multi-speaker dialogue
        has_dialogue = _is_clear_dialogue(f0_values, genders)
        
        if not has_dialogue:
            # SINGLE SPEAKER: Force ALL segments to the same gender
            print(f"[DUBBING] Single speaker detected → forcing all segments to '{majority_gender}' "
                  f"(median F0={median_f0:.0f}Hz)")
            for i, seg in enumerate(segments):
                seg["speaker"] = 0
                seg["gender"] = majority_gender
                seg["f0"] = f0_values[i]
        else:
            # MULTI-SPEAKER: Use per-segment gender classification
            print(f"[DUBBING] Multi-speaker dialogue detected → using per-segment classification")
            speaker_map = {}  # gender -> speaker_id
            next_speaker_id = 0

            for i, seg in enumerate(segments):
                gender = genders[i]
                if gender == "unknown":
                    # Try to infer from neighboring segments
                    if i > 0 and (seg["start"] - segments[i-1]["end"]) < 1.0:
                        gender = genders[i-1] if genders[i-1] != "unknown" else majority_gender
                    else:
                        gender = majority_gender

                # Assign consistent speaker ID per gender
                if gender not in speaker_map:
                    speaker_map[gender] = next_speaker_id
                    next_speaker_id += 1

                seg["speaker"] = speaker_map[gender]
                seg["gender"] = gender
                seg["f0"] = f0_values[i]

            # Refine: if segments are very close in time and have similar F0,
            # they're likely the same speaker
            for i in range(1, len(segments)):
                gap = segments[i]["start"] - segments[i-1]["end"]
                f0_curr = f0_values[i]
                f0_prev = f0_values[i-1]

                if gap < 0.5 and f0_curr > 0 and f0_prev > 0:
                    if abs(f0_curr - f0_prev) < 30:
                        segments[i]["speaker"] = segments[i-1]["speaker"]
                        segments[i]["gender"] = segments[i-1]["gender"]

        n_speakers = len(set(s["speaker"] for s in segments))
        gender_summary = {}
        for s in segments:
            g = s["gender"]
            gender_summary[g] = gender_summary.get(g, 0) + 1
        print(f"[DUBBING] Final: {n_speakers} speaker(s): {gender_summary}")
        for s in segments:
            print(f"  [{s['start']:.1f}s-{s['end']:.1f}s] Speaker {s['speaker']} ({s['gender']}, "
                  f"F0={s.get('f0', 0):.0f}Hz): \"{s['text'][:50]}\"")

        return segments

    except Exception as e:
        print(f"[DUBBING] Speaker detection failed: {e}, using single speaker")
        for seg in segments:
            seg["speaker"] = 0
            seg["gender"] = "female"
            seg["f0"] = 0
        return segments


async def _gen_tts_segment(text: str, voice: str, output_path: str, rate: str = "+0%") -> bool:
    """Generate TTS for a single segment (async)."""
    import edge_tts
    from pathlib import Path
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(output_path)
    return Path(output_path).exists() and Path(output_path).stat().st_size > 0

def _gen_tts_segment_sync(text: str, voice: str, output_path: str, rate: str = "+0%") -> bool:
    """Generate TTS for a single segment (blocking)."""
    import asyncio as _asyncio
    
    async def _gen():
        import edge_tts
        comm = edge_tts.Communicate(text, voice, rate=rate)
        await comm.save(output_path)

    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_gen())
    finally:
        loop.close()

    return Path(output_path).exists() and Path(output_path).stat().st_size > 0


def _get_audio_duration(ffmpeg: str, filepath: str) -> float:
    """Get duration of an audio file in seconds."""
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe, "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", filepath
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _build_atempo_filter(ratio: float) -> str:
    """Build atempo filter chain for FFmpeg."""
    ratio = max(0.5, min(ratio, 3.0))
    
    if 0.5 <= ratio <= 100.0:
        return f"atempo={ratio:.4f}"
    
    filters = []
    remaining = ratio
    while remaining < 0.5 or remaining > 100.0:
        if remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        else:
            filters.append("atempo=100.0")
            remaining /= 100.0
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def _dub_video_sync(ffmpeg: str, video_path: str, voice: str, target_lang: str = "vi",
                     rate: str = "+0%", pitch: str = "+0Hz", original_vol: float = 0.1,
                     voice_female: str = "vi-VN-HoaiMyNeural",
                     voice_male: str = "vi-VN-NamMinhNeural") -> dict:
    """Professional dubbing pipeline with per-segment time alignment and multi-speaker support.
    
    Pipeline:
    1. Transcribe speech with timestamps (faster-whisper, auto-detect language)
    2. Detect speakers via F0 pitch analysis + gender classification
    3. Translate each segment (SKIP if source language == target language)
    4. Generate TTS per segment with gender-appropriate voice
    5. Tempo-adjust each TTS to match original segment duration
    6. Place each TTS at exact timestamp using adelay
    7. Mix all TTS segments with voice-suppressed original audio
    
    Voice assignment rules:
    - Male speakers (F0 < 165Hz) → voice_male
    - Female speakers (F0 >= 165Hz) → voice_female
    - Same speaker always gets the same voice throughout the video
    """
    from deep_translator import GoogleTranslator

    tmp_dir = Path("D:/MyFile/tool/flow2api/tmp")

    # Step 1: Transcribe (auto-detect language)
    print(f"[DUBBING] Step 1/5: Transcribing speech (auto-detect language)...")
    transcription = _transcribe_audio_sync(ffmpeg, video_path)
    if "error" in transcription:
        return transcription
    if not transcription.get("segments"):
        return {"error": "Không phát hiện giọng nói trong video", "no_speech": True}

    segments = transcription["segments"]
    detected_lang = transcription.get("language", "unknown")
    print(f"[DUBBING] Found {len(segments)} speech segments, detected language: {detected_lang}")

    # Step 2: Detect speakers with gender classification
    print(f"[DUBBING] Step 2/5: Detecting speakers & gender via F0 analysis...")
    segments = _detect_speakers(ffmpeg, video_path, segments)

    # Build voice mapping: gender → voice
    # Each speaker gets a voice based on their detected gender
    speaker_voice_map = {}  # speaker_id → voice
    for seg in segments:
        spk = seg.get("speaker", 0)
        if spk not in speaker_voice_map:
            gender = seg.get("gender", "female")
            if gender == "male":
                speaker_voice_map[spk] = voice_male
            else:
                speaker_voice_map[spk] = voice_female

    print(f"[DUBBING] Voice assignment:")
    for spk, voice_name in speaker_voice_map.items():
        gender = next((s["gender"] for s in segments if s["speaker"] == spk), "unknown")
        print(f"  Speaker {spk} ({gender}) → {voice_name}")

    # Step 3: Translate each segment (SKIP if source == target language)
    # Normalize language codes for comparison
    _lang_normalize = {"zh": "zh-CN", "chinese": "zh-CN"}
    source_lang = _lang_normalize.get(detected_lang, detected_lang)
    # Check if source language matches target (e.g., both are Vietnamese)
    skip_translation = (source_lang == target_lang) or \
                       (detected_lang in (target_lang,)) or \
                       (target_lang == "vi" and detected_lang in ("vi", "vietnamese"))
    
    if skip_translation:
        print(f"[DUBBING] Step 3/5: SKIPPING translation — source ({detected_lang}) == target ({target_lang})")
        print(f"[DUBBING] Will re-voice with TTS only (no translation needed)")
        for seg in segments:
            seg["translated"] = seg["text"]  # Use original text as-is
            print(f"  KEEP: \"{seg['text'][:60]}\"")
    else:
        print(f"[DUBBING] Step 3/5: Translating {len(segments)} segments ({detected_lang} → {target_lang})...")
        translator = GoogleTranslator(source=detected_lang, target=target_lang)
        for seg in segments:
            try:
                seg["translated"] = translator.translate(seg["text"])
            except Exception as e:
                seg["translated"] = seg["text"]
                print(f"[DUBBING] Translation failed for segment: {e}")
            print(f"  \"{seg['text'][:40]}\" -> \"{seg['translated'][:40]}\"")

    # Step 4: Generate TTS per segment + tempo adjustment
    print(f"[DUBBING] Step 4/5: Generating TTS per segment...")
    tts_files = []
    for i, seg in enumerate(segments):
        tts_path = str(tmp_dir / f"_dub_seg_{i}_{int(time.time())}.mp3")
        seg_voice = speaker_voice_map.get(seg.get("speaker", 0), voice_female)
        
        success = _gen_tts_segment_sync(seg["translated"], seg_voice, tts_path, rate)
        if not success:
            print(f"  [WARN] TTS failed for segment {i}, skipping")
            continue

        tts_duration = _get_audio_duration(ffmpeg, tts_path)
        original_duration = seg["duration"]

        if tts_duration > 0 and original_duration > 0:
            tempo_ratio = tts_duration / original_duration
            tempo_ratio = max(0.7, min(tempo_ratio, 1.5))
        else:
            tempo_ratio = 1.0

        tts_files.append({
            "path": tts_path,
            "start": seg["start"],
            "end": seg["end"],
            "original_duration": original_duration,
            "tts_duration": tts_duration,
            "tempo": tempo_ratio,
            "speaker": seg.get("speaker", 0),
            "gender": seg.get("gender", "female"),
            "text": seg["translated"],
        })
        print(f"  Seg {i}: {seg['start']:.1f}s-{seg['end']:.1f}s, "
              f"speaker={seg.get('speaker', 0)} ({seg.get('gender', '?')}), "
              f"tempo={tempo_ratio:.2f}x, voice={seg_voice.split('-')[-1]}")

    if not tts_files:
        return {"error": "Không tạo được TTS cho bất kỳ đoạn nào"}

    # Step 5: Build FFmpeg command with per-segment placement
    print(f"[DUBBING] Step 5/5: Building final audio mix ({len(tts_files)} segments)...")
    output_name = f"dubbed_{int(time.time())}.mp4"
    output_path = str(tmp_dir / output_name)

    inputs = ["-i", video_path]
    for tf in tts_files:
        inputs.extend(["-i", tf["path"]])

    filter_parts = [
        f"[0:a]highpass=f=4000,volume={original_vol}[bg_high]",
        f"[0:a]lowpass=f=300,volume={original_vol}[bg_low]",
        f"[bg_high][bg_low]amix=inputs=2:duration=longest[bg]",
    ]

    seg_labels = []
    for i, tf in enumerate(tts_files):
        input_idx = i + 1
        atempo = _build_atempo_filter(tf["tempo"])
        delay_ms = int(tf["start"] * 1000)
        label = f"s{i}"
        filter_parts.append(
            f"[{input_idx}:a]{atempo},adelay={delay_ms}|{delay_ms},volume=1.0[{label}]"
        )
        seg_labels.append(f"[{label}]")

    if len(seg_labels) > 1:
        mix_input = "".join(seg_labels)
        filter_parts.append(
            f"{mix_input}amix=inputs={len(seg_labels)}:duration=longest:normalize=0[all_tts]"
        )
        tts_label = "[all_tts]"
    else:
        tts_label = seg_labels[0]

    filter_parts.append(
        f"[bg]{tts_label}amix=inputs=2:duration=first:dropout_transition=1[a]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = [
        ffmpeg, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]

    print(f"[DUBBING] Running FFmpeg with {len(tts_files)} TTS inputs...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    for tf in tts_files:
        try:
            Path(tf["path"]).unlink()
        except Exception:
            pass

    if result.returncode != 0:
        err = result.stderr[-500:] if result.stderr else "unknown"
        print(f"[DUBBING] FFmpeg error: {err}")
        return {"error": f"FFmpeg dubbing failed: {err}"}

    original_text = " ".join(s["text"] for s in segments)
    translated_text = " ".join(s["translated"] for s in segments)
    n_speakers = len(set(s.get("speaker", 0) for s in segments))

    # Build speaker details for response
    speaker_details = {}
    for seg in segments:
        spk = seg.get("speaker", 0)
        if spk not in speaker_details:
            speaker_details[spk] = {
                "gender": seg.get("gender", "unknown"),
                "voice": speaker_voice_map.get(spk, "unknown"),
                "segments": 0,
            }
        speaker_details[spk]["segments"] += 1

    print(f"[DUBBING] Done! {len(tts_files)} segments, {n_speakers} speaker(s)")
    for spk, info in speaker_details.items():
        print(f"  Speaker {spk}: {info['gender']}, voice={info['voice']}, {info['segments']} segments")

    return {
        "success": True,
        "output_name": output_name,
        "original_text": original_text,
        "translated_text": translated_text,
        "segments_count": len(segments),
        "speakers_count": n_speakers,
        "speaker_details": {str(k): v for k, v in speaker_details.items()},
    }


@router.post("/api/tts/dub")
async def dub_video(request: Request):
    """Professional video dubbing: detect speech, identify speakers, translate, and replace voices.
    
    Body:
        video_url: path to the video
        voice_female: female Vietnamese voice (default: vi-VN-HoaiMyNeural)
        voice_male: male Vietnamese voice (default: vi-VN-NamMinhNeural)
        original_volume: background audio volume 0-1 (default: 0.1)
    """
    try:
        body = await request.json()
        video_url = body.get("video_url", "")
        voice_female = body.get("voice_female", body.get("voice", "vi-VN-HoaiMyNeural"))
        voice_male = body.get("voice_male", "vi-VN-NamMinhNeural")
        original_volume = float(body.get("original_volume", 0.1))

        if not video_url:
            return JSONResponse({"error": "Cần video_url"}, status_code=400)

        base_dir = Path("D:/MyFile/tool/flow2api")
        if video_url.startswith("/tmp/"):
            video_path = str(base_dir / video_url.lstrip("/"))
        else:
            video_path = video_url

        if not Path(video_path).exists():
            return JSONResponse({"error": f"Video không tồn tại: {video_url}"}, status_code=404)

        ffmpeg = _get_ffmpeg_path()
        result = await asyncio.to_thread(
            _dub_video_sync, ffmpeg, video_path, voice_female, "vi",
            "+0%", "+0Hz", original_volume, voice_female, voice_male
        )

        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=500)

        output_name = result["output_name"]
        final_path = Path("D:/MyFile/tool/flow2api/tmp") / output_name
        size_mb = round(final_path.stat().st_size / 1024 / 1024, 1)
        return JSONResponse({
            "success": True,
            "url": f"/tmp/{output_name}",
            "size_mb": size_mb,
            "original_text": result["original_text"],
            "translated_text": result["translated_text"],
            "segments_count": result["segments_count"],
            "speakers_count": result["speakers_count"],
            "speaker_details": result.get("speaker_details", {}),
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
