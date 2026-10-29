# -*- coding: utf-8 -*-
"""
书生 API 原生 Function Calling 实测脚本（兴智杯 Step 0 验收）

用途：10 分钟内确定书生端点是否支持原生 tool calling，
结论直接决定 Codex 接入配置走 LiteLLM 转换还是 prompt-JSON 降级。

用法：
  set INTERN_API_BASE=https://你的端点/v1
  set INTERN_API_KEY=你的key
  set INTERN_MODEL=intern-s2        （以平台实际模型名为准，测错会自动列出可用模型）
  python test_intern_function_calling.py

三个用例：
  T1 单工具调用      -> 期望 message.tool_calls 结构化返回
  T2 工具结果回填    -> 期望模型消化 tool 结果后生成最终回答
  T3 流式工具调用    -> 期望分片拼装出完整 tool_calls（不支持流式不影响 codex exec 非交互模式）
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("INTERN_API_BASE", "").rstrip("/")
KEY = os.environ.get("INTERN_API_KEY", "")
MODEL = os.environ.get("INTERN_MODEL", "intern-s2")
TIMEOUT = 60

if not BASE or not KEY:
    print("[FAIL] 请先设置环境变量 INTERN_API_BASE 和 INTERN_API_KEY")
    sys.exit(1)

URL = BASE + "/chat/completions"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_sales_summary",
        "description": "返回指定商品近 N 天的销售汇总（日销量、库存、缺货风险）",
        "parameters": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "商品编号"},
                "days": {"type": "integer", "description": "统计天数", "default": 28},
            },
            "required": ["sku"],
        },
    },
}]


def post(payload, stream=False):
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + KEY,
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def list_models():
    try:
        req = urllib.request.Request(
            BASE + "/models",
            headers={"Authorization": "Bearer " + KEY},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", [])]
        print("[HINT] 当前端点可用模型:", ids)
    except Exception as e:
        print("[HINT] 无法列出模型:", e)


def t1_single_tool_call():
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "帮我查一下 SKU A001 最近 28 天的销售汇总"}],
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    try:
        with post(payload) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:500]
        print("[T1] HTTP", e.code, body)
        if e.code in (400, 404) and "model" in body.lower():
            list_models()
        return False

    msg = data["choices"][0]["message"]
    finish = data["choices"][0].get("finish_reason")
    tcs = msg.get("tool_calls")
    if tcs:
        tc = tcs[0]
        fn = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except Exception:
            print("[T1] FAIL tool_calls 存在但 arguments 不是合法 JSON:", tc["function"]["arguments"])
            return False
        print("[T1] PASS 原生 tool_calls | name=%s args=%s finish=%s id=%s"
              % (fn, args, finish, tc.get("id")))
        return {"id": tc.get("id"), "name": fn, "args": args}
    print("[T1] FAIL 无 tool_calls，模型把内容写进了 message.content：")
    print("     content 前 200 字:", (msg.get("content") or "")[:200])
    return False


def t2_tool_result_backfill(first):
    tool_result = json.dumps({
        "sku": "A001", "days": 28, "avg_daily_sales": 12.4,
        "stock": 85, "stockout_risk": "low",
    }, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "帮我查一下 SKU A001 最近 28 天的销售汇总"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": first["id"] or "call_0",
            "type": "function",
            "function": {"name": first["name"],
                         "arguments": json.dumps(first["args"], ensure_ascii=False)},
        }]},
        {"role": "tool", "tool_call_id": first["id"] or "call_0", "content": tool_result},
    ]
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS}
    try:
        with post(payload) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("[T2] HTTP", e.code, e.read().decode("utf-8", "ignore")[:500])
        return False
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if content:
        ok = ("12.4" in content) or ("缺货" in content) or ("风险" in content)
        print("[T2] %s 消化了工具结果 | finish=%s | 回答前 120 字: %s"
              % ("PASS" if ok else "WEAK", data["choices"][0].get("finish_reason"), content[:120]))
        return True
    print("[T2] FAIL 回填 tool 结果后 content 为空, message:", json.dumps(msg, ensure_ascii=False)[:300])
    return False


def t3_streaming_tool_call():
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "查 SKU B002 近 28 天销售汇总"}],
        "tools": TOOLS,
        "stream": True,
    }
    try:
        resp = post(payload, stream=True)
    except urllib.error.HTTPError as e:
        print("[T3] HTTP", e.code, "（流式不支持不阻塞主流程，codex exec 走非交互模式）")
        return None
    name_buf, arg_buf, saw_tool = "", "", False
    try:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except Exception:
                continue
            for ch in chunk.get("choices", []):
                delta = ch.get("delta") or {}
                for tc in delta.get("tool_calls") or []:
                    saw_tool = True
                    fn = tc.get("function") or {}
                    name_buf += fn.get("name") or ""
                    arg_buf += fn.get("arguments") or ""
    except Exception as e:
        print("[T3] WARN 流式读取异常:", e)
        return None
    if saw_tool and name_buf:
        try:
            json.loads(arg_buf)
            print("[T3] PASS 流式分片拼装完整 | name=%s args=%s" % (name_buf, arg_buf[:80]))
            return True
        except Exception:
            print("[T3] FAIL 分片拼出的 arguments 不完整:", arg_buf[:120])
            return False
    print("[T3] WARN 流式响应中未出现 tool_calls 分片")
    return None


def main():
    print("=" * 60)
    print("端点:", URL)
    print("模型:", MODEL)
    print("=" * 60)
    first = t1_single_tool_call()
    results = {"T1 单工具调用": bool(first)}
    if isinstance(first, dict):
        results["T2 工具结果回填"] = t2_tool_result_backfill(first)
    else:
        results["T2 工具结果回填"] = "跳过（T1 未通过）"
    results["T3 流式工具调用"] = t3_streaming_tool_call()

    print("=" * 60)
    for k, v in results.items():
        print(k, "->", v)
    t1p = results["T1 单工具调用"]
    if t1p:
        print("\n[结论] 原生 function calling 可用 -> Codex 走 LiteLLM 转换配置（use_chat_completions_api）")
    else:
        print("\n[结论] 原生 function calling 不可用 -> Codex 接入需降级为 prompt 内 JSON 工具协议，"
              "或改用自研 agent loop（复用 math_agent）")
    print("=" * 60)


if __name__ == "__main__":
    main()
