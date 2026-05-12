"""Admin captcha configuration and score test endpoints."""
import asyncio, time
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Any, Dict, Optional
from curl_cffi.requests import AsyncSession
from ..core.config import config, get_yescaptcha_min_score, normalize_yescaptcha_task_type
from . import _admin_deps as deps
from .admin_auth import verify_admin_token
from .admin_utils import (
    SUPPORTED_API_CAPTCHA_METHODS, _mask_token, _build_proxy_map,
    _normalize_http_base_url, _get_remote_browser_client_config,
    _sync_json_http_request, _guess_client_hints_from_user_agent,
    _guess_impersonate_from_user_agent,
)

router = APIRouter()

class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False

async def _resolve_score_test_verify_proxy(captcha_method, browser_proxy_enabled, browser_proxy_url):
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        pm = _build_proxy_map(browser_proxy_url)
        if pm: return pm, True, "captcha_browser_proxy", browser_proxy_url
    try:
        if deps.proxy_manager:
            pc = await deps.proxy_manager.get_proxy_config()
            if pc and pc.enabled and pc.proxy_url:
                pm = _build_proxy_map(pc.proxy_url)
                if pm: return pm, True, "request_proxy", pc.proxy_url
    except: pass
    return None, False, "none", ""

async def _solve_recaptcha_with_api_service(method, website_url, website_key, action, enterprise=False):
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key; base_url = config.yescaptcha_base_url
        task_type = config.yescaptcha_task_type; min_score = get_yescaptcha_min_score(task_type)
    elif method == "capmonster":
        client_key = config.capmonster_api_key; base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"; min_score = None
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key; base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"; min_score = None
    elif method == "capsolver":
        client_key = config.capsolver_api_key; base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"; min_score = None
    else: raise RuntimeError(f"不支持的打码方式: {method}")
    if not client_key: raise RuntimeError(f"{method} API Key 未配置")
    task = {"websiteURL": website_url, "websiteKey": website_key, "type": task_type, "pageAction": action}
    if min_score is not None: task["minScore"] = min_score
    if enterprise and method == "capsolver": task["isEnterprise"] = True
    create_url = f"{base_url.rstrip('/')}/createTask"; get_url = f"{base_url.rstrip('/')}/getTaskResult"
    proxies = None
    try:
        if deps.proxy_manager:
            pc = await deps.proxy_manager.get_proxy_config()
            if pc and pc.enabled and pc.proxy_url: proxies = {"http": pc.proxy_url, "https": pc.proxy_url}
    except: pass
    async with AsyncSession() as session:
        cr = await session.post(create_url, json={"clientKey": client_key, "task": task}, impersonate="chrome120", timeout=30, proxies=proxies)
        cj = cr.json(); task_id = cj.get("taskId")
        if not task_id: raise RuntimeError(f"{method} createTask 失败: {cj.get('errorDescription') or cj.get('errorMessage') or str(cj)}")
        for _ in range(40):
            pr = await session.post(get_url, json={"clientKey": client_key, "taskId": task_id}, impersonate="chrome120", timeout=30, proxies=proxies)
            pj = pr.json()
            if pj.get("status") == "ready":
                sol = pj.get("solution", {}) or {}; tok = sol.get("gRecaptchaResponse") or sol.get("token")
                if tok: return tok
                raise RuntimeError(f"{method} 返回结果缺少 token: {pj}")
            if pj.get("errorId") not in (None, 0): raise RuntimeError(f"{method} getTaskResult 失败: {pj.get('errorDescription') or str(pj)}")
            await asyncio.sleep(3)
    raise RuntimeError(f"{method} 获取 token 超时")

async def _score_test_with_remote_browser_service(website_url, website_key, verify_url, action, enterprise=False):
    base_url, api_key, timeout = _get_remote_browser_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    payload = {"website_url": website_url, "website_key": website_key, "verify_url": verify_url, "action": action, "enterprise": enterprise}
    sc, rp, rt = await _sync_json_http_request(method="POST", url=endpoint, headers={"Authorization": f"Bearer {api_key}"}, payload=payload, timeout=timeout)
    if sc >= 400:
        detail = (rp.get("detail") or rp.get("message") or str(rp)) if isinstance(rp, dict) else (rt or "").strip()
        raise RuntimeError(f"远程打码服务请求失败 (HTTP {sc}): {detail or '未知错误'}")
    if not isinstance(rp, dict): raise RuntimeError("远程打码服务返回格式错误")
    return rp

@router.post("/api/captcha/config")
async def update_captcha_config(request: dict, token: str = Depends(verify_admin_token)):
    from ..services.browser_captcha import validate_browser_proxy_url
    cm = request.get("captcha_method"); bpe = request.get("browser_proxy_enabled", False); bpu = request.get("browser_proxy_url", "")
    rbu = request.get("remote_browser_base_url"); rbk = request.get("remote_browser_api_key"); rbt = request.get("remote_browser_timeout", 60)
    bc = request.get("browser_count", 1)
    bpfrens = request.get("browser_personal_fresh_restart_every_n_solves", 10)
    if bpe and bpu:
        ok, msg = validate_browser_proxy_url(bpu)
        if not ok: return {"success": False, "message": msg}
    if rbu:
        try: rbu = _normalize_http_base_url(rbu)
        except RuntimeError as e: return {"success": False, "message": str(e)}
    try: rbt = max(5, int(rbt or 60))
    except: return {"success": False, "message": "远程打码超时时间必须是整数秒"}
    try: bc = max(1, min(20, int(bc or 1)))
    except: return {"success": False, "message": "浏览器实例数量必须是整数"}
    try: bpfrens = max(0, int(bpfrens if bpfrens is not None else 10))
    except: return {"success": False, "message": "重置码数必须是整数，0 表示禁用"}
    if cm == "remote_browser":
        if not (rbu or "").strip(): return {"success": False, "message": "remote_browser 模式需要配置远程打码服务地址"}
        if not (rbk or "").strip(): return {"success": False, "message": "remote_browser 模式需要配置远程打码服务 API Key"}
    ytt = normalize_yescaptcha_task_type(request.get("yescaptcha_task_type"))
    await deps.db.update_captcha_config(
        captcha_method=cm, yescaptcha_api_key=request.get("yescaptcha_api_key"), yescaptcha_base_url=request.get("yescaptcha_base_url"), yescaptcha_task_type=ytt,
        capmonster_api_key=request.get("capmonster_api_key"), capmonster_base_url=request.get("capmonster_base_url"),
        ezcaptcha_api_key=request.get("ezcaptcha_api_key"), ezcaptcha_base_url=request.get("ezcaptcha_base_url"),
        capsolver_api_key=request.get("capsolver_api_key"), capsolver_base_url=request.get("capsolver_base_url"),
        remote_browser_base_url=rbu, remote_browser_api_key=rbk, remote_browser_timeout=rbt,
        browser_proxy_enabled=bpe, browser_proxy_url=bpu if bpe else None, browser_count=bc,
        personal_project_pool_size=request.get("personal_project_pool_size"), personal_max_resident_tabs=request.get("personal_max_resident_tabs"),
        browser_personal_fresh_restart_every_n_solves=bpfrens, personal_idle_tab_ttl_seconds=request.get("personal_idle_tab_ttl_seconds"),
        personal_headless=request.get("personal_headless"))
    await deps.db.reload_config_to_memory()
    if cm == "browser":
        try:
            from ..services.browser_captcha import BrowserCaptchaService
            s = await BrowserCaptchaService.get_instance(deps.db); await s.reload_browser_count()
        except: pass
    if cm == "personal":
        try:
            from ..services.browser_captcha_personal import BrowserCaptchaService
            s = await BrowserCaptchaService.get_instance(deps.db); await s.reload_config()
        except Exception as e: print(f"[Admin] Personal 配置热更新失败: {e}")
    return {"success": True, "message": "验证码配置更新成功"}

@router.get("/api/captcha/config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    cc = await deps.db.get_captcha_config()
    return {"captcha_method": cc.captcha_method, "yescaptcha_api_key": cc.yescaptcha_api_key, "yescaptcha_base_url": cc.yescaptcha_base_url, "yescaptcha_task_type": cc.yescaptcha_task_type,
        "capmonster_api_key": cc.capmonster_api_key, "capmonster_base_url": cc.capmonster_base_url, "ezcaptcha_api_key": cc.ezcaptcha_api_key, "ezcaptcha_base_url": cc.ezcaptcha_base_url,
        "capsolver_api_key": cc.capsolver_api_key, "capsolver_base_url": cc.capsolver_base_url, "remote_browser_base_url": cc.remote_browser_base_url, "remote_browser_api_key": cc.remote_browser_api_key,
        "remote_browser_timeout": cc.remote_browser_timeout, "browser_proxy_enabled": cc.browser_proxy_enabled, "browser_proxy_url": cc.browser_proxy_url or "", "browser_count": cc.browser_count,
        "personal_project_pool_size": cc.personal_project_pool_size, "personal_max_resident_tabs": cc.personal_max_resident_tabs,
        "browser_personal_fresh_restart_every_n_solves": cc.browser_personal_fresh_restart_every_n_solves, "personal_idle_tab_ttl_seconds": cc.personal_idle_tab_ttl_seconds,
        "personal_headless": cc.personal_headless}

@router.post("/api/captcha/score-test")
async def test_captcha_score(_request: Optional[CaptchaScoreTestRequest] = None, _token: str = Depends(verify_admin_token)):
    req = _request or CaptchaScoreTestRequest()
    website_url = (req.website_url or "https://antcpt.com/score_detector/").strip()
    website_key = (req.website_key or "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf").strip()
    action = (req.action or "homepage").strip(); verify_url = (req.verify_url or "https://antcpt.com/score_detector/verify.php").strip()
    enterprise = bool(req.enterprise); started_at = time.time()
    cc = await deps.db.get_captcha_config(); captcha_method = (cc.captcha_method or config.captcha_method or "").strip().lower()
    bpe = bool(cc.browser_proxy_enabled); bpu = cc.browser_proxy_url or ""
    token_value = None; fingerprint = None; token_elapsed_ms = 0; verify_elapsed_ms = 0; verify_http_status = None
    verify_result = {}; verify_headers = {}; verify_proxy_used = False; verify_proxy_source = "none"; verify_proxy_url = ""; verify_impersonate = "chrome120"
    page_verify_only = captcha_method in {"browser", "personal", "remote_browser"}; verify_mode = "browser_page" if page_verify_only else "server_post"
    def _result(success, message, **extra):
        return {"success": success, "message": message, "captcha_method": captcha_method, "website_url": website_url, "website_key": website_key,
            "action": action, "verify_url": verify_url, "enterprise": enterprise, "token_acquired": bool(token_value),
            "token_preview": _mask_token(token_value), "token_elapsed_ms": token_elapsed_ms, "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status, "verify_result": verify_result,
            "verify_request_meta": {"mode": verify_mode, "proxy_used": verify_proxy_used, "user_agent": verify_headers.get("user-agent",""),
                "accept_language": verify_headers.get("accept-language",""), "sec_ch_ua": verify_headers.get("sec-ch-ua",""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile",""), "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform",""),
                "origin": verify_headers.get("origin",""), "referer": verify_headers.get("referer",""),
                "x_requested_with": verify_headers.get("x-requested-with",""), "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url, "impersonate": verify_impersonate},
            "browser_proxy_enabled": bpe, "browser_proxy_url": bpu if bpe else "", "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000), **extra}
    try:
        token_start = time.time()
        if captcha_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService
            svc = await BrowserCaptchaService.get_instance(deps.db)
            sp, bid = await svc.get_custom_score(website_url=website_url, website_key=website_key, verify_url=verify_url, action=action, enterprise=enterprise)
            if isinstance(sp, dict):
                token_value = sp.get("token"); verify_elapsed_ms = int(sp.get("verify_elapsed_ms") or 0); verify_http_status = sp.get("verify_http_status")
                verify_result = sp.get("verify_result") if isinstance(sp.get("verify_result"), dict) else {}; verify_mode = sp.get("verify_mode") or "browser_page"
                ste = sp.get("token_elapsed_ms")
                if isinstance(ste, (int, float)): token_elapsed_ms = int(ste)
            if token_value:
                fingerprint = await svc.get_fingerprint(bid); verify_proxy_used = bool(bpe and bpu)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"; verify_proxy_url = bpu if verify_proxy_used else ""
        elif captcha_method == "personal":
            from ..services.browser_captcha_personal import BrowserCaptchaService
            svc = await BrowserCaptchaService.get_instance(deps.db)
            sp = await svc.get_custom_score(website_url=website_url, website_key=website_key, verify_url=verify_url, action=action, enterprise=enterprise)
            if isinstance(sp, dict):
                token_value = sp.get("token"); verify_elapsed_ms = int(sp.get("verify_elapsed_ms") or 0); verify_http_status = sp.get("verify_http_status")
                verify_result = sp.get("verify_result") if isinstance(sp.get("verify_result"), dict) else {}; verify_mode = sp.get("verify_mode") or "browser_page"
                ste = sp.get("token_elapsed_ms")
                if isinstance(ste, (int, float)): token_elapsed_ms = int(ste)
            if token_value:
                fingerprint = svc.get_last_fingerprint(); verify_proxy_used = bool(bpe and bpu)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"; verify_proxy_url = bpu if verify_proxy_used else ""
        elif captcha_method == "remote_browser":
            sp = await _score_test_with_remote_browser_service(website_url=website_url, website_key=website_key, verify_url=verify_url, action=action, enterprise=enterprise)
            if isinstance(sp, dict):
                if sp.get("success") is False: raise RuntimeError(sp.get("message") or "远程打码分数测试失败")
                token_value = sp.get("token"); verify_elapsed_ms = int(sp.get("verify_elapsed_ms") or 0); verify_http_status = sp.get("verify_http_status")
                verify_result = sp.get("verify_result") if isinstance(sp.get("verify_result"), dict) else {}; verify_mode = sp.get("verify_mode") or "remote_browser_page"
                ste = sp.get("token_elapsed_ms")
                if isinstance(ste, (int, float)): token_elapsed_ms = int(ste)
                fingerprint = sp.get("fingerprint") if isinstance(sp.get("fingerprint"), dict) else None
        elif captcha_method in SUPPORTED_API_CAPTCHA_METHODS:
            if captcha_method == "capsolver" and "antcpt.com" in website_url:
                token_value = await _solve_recaptcha_with_api_service(method=captcha_method, website_url="https://labs.google/", website_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", action="IMAGE_GENERATION", enterprise=True)
                if token_value:
                    if token_elapsed_ms <= 0: token_elapsed_ms = int((time.time() - token_start) * 1000)
                    return _result(True, "CapSolver不支持antcpt。已成功用 Google Labs 测试连通性", score=0.9)
            else: token_value = await _solve_recaptcha_with_api_service(method=captcha_method, website_url=website_url, website_key=website_key, action=action, enterprise=enterprise)
        else: return _result(False, f"当前打码方式不支持分数测试: {captcha_method}")
        if token_elapsed_ms <= 0: token_elapsed_ms = int((time.time() - token_start) * 1000)
        if captcha_method == "remote_browser" and not token_value and isinstance(verify_result, dict):
            if verify_result.get("success") is True: token_value = verify_result.get("token") or verify_result.get("gRecaptchaResponse") or "__verified_by_remote__"
        if not token_value: return _result(False, "未获取到 reCAPTCHA token")
        if verify_mode == "server_post" and not page_verify_only:
            vs = time.time()
            verify_headers = {"accept": "application/json, text/javascript, */*; q=0.01", "content-type": "application/json", "origin": "https://antcpt.com", "referer": website_url, "x-requested-with": "XMLHttpRequest"}
            if isinstance(fingerprint, dict):
                for hk, fk in [("user-agent","user_agent"),("accept-language","accept_language"),("sec-ch-ua","sec_ch_ua"),("sec-ch-ua-mobile","sec_ch_ua_mobile"),("sec-ch-ua-platform","sec_ch_ua_platform")]:
                    v = (fingerprint.get(fk) or "").strip()
                    if v: verify_headers[hk] = v
            if verify_headers.get("user-agent"):
                for hn, hv in _guess_client_hints_from_user_agent(verify_headers.get("user-agent", "")).items():
                    if hv and not verify_headers.get(hn): verify_headers[hn] = hv
                verify_impersonate = _guess_impersonate_from_user_agent(verify_headers.get("user-agent", ""))
            vp, verify_proxy_used, verify_proxy_source, verify_proxy_url = await _resolve_score_test_verify_proxy(captcha_method, bpe, bpu)
            async with AsyncSession() as sess:
                vr = await sess.post(verify_url, json={"g-recaptcha-response": token_value}, headers=verify_headers, proxies=vp, impersonate=verify_impersonate, timeout=30)
            verify_elapsed_ms = int((time.time() - vs) * 1000); verify_http_status = vr.status_code
            try: verify_result = vr.json()
            except: verify_result = {"raw": vr.text}
        else:
            verify_headers = {"origin": "https://antcpt.com", "referer": website_url, "x-requested-with": "XMLHttpRequest"}
            if isinstance(fingerprint, dict):
                verify_headers.update({"user-agent": fingerprint.get("user_agent",""), "accept-language": fingerprint.get("accept_language",""), "sec-ch-ua": fingerprint.get("sec_ch_ua",""), "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile",""), "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform","")})
        vs = bool(verify_result.get("success")) if isinstance(verify_result, dict) else False
        sc = verify_result.get("score") if isinstance(verify_result, dict) else None
        return _result(vs, "分数校验成功" if vs else "分数校验未通过", score=sc)
    except Exception as e:
        return _result(False, f"分数测试失败: {str(e)}")
