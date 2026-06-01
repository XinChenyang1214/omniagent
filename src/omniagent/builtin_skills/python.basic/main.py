from __future__ import annotations


def run(payload, context):
    sandbox = context["sandbox"]
    result = sandbox.run_python(str(payload.get("code", "")))
    return {
        "success": bool(result.get("ok")),
        "status": "completed" if result.get("ok") else "failed",
        "message": "Python Skill 执行完成"
        if result.get("ok")
        else str(result.get("error", "Python Skill 执行失败")),
        "output": result,
        "logs": [str(result.get("traceback", ""))] if not result.get("ok") else [],
    }
