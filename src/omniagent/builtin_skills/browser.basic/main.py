from __future__ import annotations


_BLOCKED_CONTENT_TOKENS = (
    "百度安全验证",
    "安全验证",
    "人机验证",
    "验证码",
    "网络不给力，请稍后重试",
    "verify you are human",
    "captcha",
    "access denied",
    "forbidden",
    "just a moment",
    "enable javascript",
    "unusual traffic",
    "location.replace(location.href.replace",
    "http-equiv=\"refresh\"",
    "url=http://www.baidu.com/",
)


def _looks_blocked(body: str) -> bool:
    lowered = body.lower()
    return any(token.lower() in lowered for token in _BLOCKED_CONTENT_TOKENS)


def run(payload, context):
    sandbox = context["sandbox"]
    response = sandbox.fetch_url(str(payload.get("url", "")))
    body = str(response.get("body", ""))
    if _looks_blocked(body):
        return {
            "success": False,
            "status": "failed",
            "message": "Browser Skill 抓取被安全验证、验证码或访问限制拦截，未获得可用页面内容",
            "output": {
                **response,
                "blocked": True,
                "blocked_reason": "access_verification_or_denial",
            },
            "error_code": "execution_error",
        }
    return {
        "success": True,
        "status": "completed",
        "message": f"Browser Skill 抓取完成: {response.get('url')}",
        "output": response,
    }
