from __future__ import annotations

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.base import BaseExecutor
from omniagent.models import ArtifactRecord, ExecutionResult, TaskRunContext, utc_now


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


class BrowserExecutor(BaseExecutor):
    name = "browser"
    # META-001: keep this in sync with builtin_skills/browser.basic/skill.yaml.
    supported_actions = ("fetch",)

    def execute(self, context: TaskRunContext) -> ExecutionResult:
        url = str(context.task_request.payload.get("url", ""))
        if not url:
            raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "browser executor 需要 url")

        started_at = utc_now()
        response = context.sandbox.fetch_url(url)
        preview_path = context.task_dir / "artifacts" / "browser_body.html"
        body = str(response.get("body", ""))
        preview_path.write_text(body, encoding="utf-8")
        if _looks_blocked(body):
            return ExecutionResult(
                success=False,
                status="failed",
                message="抓取页面被安全验证、验证码或访问限制拦截，未获得可用页面内容",
                output={
                    "url": response.get("url"),
                    "status": response.get("status"),
                    "body": body,
                    "source": response.get("source"),
                    "blocked": True,
                    "blocked_reason": "access_verification_or_denial",
                },
                artifacts=[
                    ArtifactRecord(kind="html", path=str(preview_path), description="页面内容快照"),
                ],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
                error_code=ErrorCode.EXECUTION_ERROR,
            )
        return ExecutionResult(
            success=True,
            status="completed",
            message=f"抓取页面成功: {url}",
            output={
                "url": response.get("url"),
                "status": response.get("status"),
                "body": response.get("body"),
                "source": response.get("source"),
            },
            artifacts=[
                ArtifactRecord(kind="html", path=str(preview_path), description="页面内容快照"),
            ],
            executor=self.name,
            started_at=started_at,
            finished_at=utc_now(),
        )
