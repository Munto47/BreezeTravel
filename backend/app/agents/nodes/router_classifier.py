"""
Sprint 3 — 微调 Router 本地推理模块

职责：加载 Qwen2.5-1.5B LoRA 适配器，对用户查询做快速意图分类。
作为 DeepSeek API 调用的 fast path：本地推理延迟 ~2s（GPU），无网络往返开销。

用法（已在 router.py 中自动集成，通过环境变量开关）：
  FT_ROUTER_ENABLED=true        启用本地分类器
  FT_ROUTER_MODEL_PATH=...      LoRA 适配器路径（默认 models/router_lora）

返回值格式：
  {"intent": "amap"|"rag"|"both"|"weather", "query_rewrite": "..."}

若模型加载失败或推理异常，自动降级到 DeepSeek API（无感知 fallback）。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_model = None
_tokenizer = None
_loaded = False
_load_error: Optional[str] = None

VALID_INTENTS = {"amap", "rag", "both", "weather"}

_CLASSIFY_SYSTEM = (
    "你是一个旅行意图分类器。根据用户查询，输出 JSON：\n"
    '{"intent": "amap"|"rag"|"both"|"weather", "query_rewrite": "改写后的查询"}\n'
    "意图定义：\n"
    "- amap   : 找具体地点/POI（景点/餐厅/住宿）\n"
    "- rag    : 求攻略/游记/避坑经验\n"
    "- both   : 找口碑好的地点（同时需要地点和评价）\n"
    "- weather: 询问天气/季节/穿衣建议\n"
    "只输出 JSON，不要解释。"
)


def _try_load(model_path: str) -> tuple[bool, Optional[str]]:
    """尝试加载 LoRA 模型，返回 (success, error_msg)"""
    global _model, _tokenizer, _loaded, _load_error

    path = Path(model_path)
    if not path.exists():
        return False, f"模型路径不存在: {model_path}"

    meta_path = path / "adapter_meta.json"
    if not meta_path.exists():
        return False, f"缺少 adapter_meta.json，请先运行 train_router.py"

    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        base_model_id = meta.get("base_model", "Qwen/Qwen2.5-1.5B-Instruct")

        print(f"[RouterClassifier] 加载基础模型: {base_model_id}")
        device = "cuda" if torch.cuda.is_available() else "cpu"

        tokenizer = AutoTokenizer.from_pretrained(
            str(path),  # LoRA 目录已包含 tokenizer
            trust_remote_code=True,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        base = AutoModelForCausalLM.from_pretrained(
            base_model_id,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            device_map="auto" if device == "cuda" else None,
            trust_remote_code=True,
        )
        model = PeftModel.from_pretrained(base, str(path))
        model.eval()

        _tokenizer = tokenizer
        _model = model
        _loaded = True
        print(f"[RouterClassifier] 模型加载成功（device={device}）")
        return True, None

    except Exception as exc:
        err = f"模型加载失败: {exc}"
        _load_error = err
        return False, err


def ensure_loaded(model_path: str) -> bool:
    """线程安全的懒加载，返回是否成功"""
    global _loaded
    if _loaded:
        return True

    with _lock:
        if _loaded:
            return True
        success, err = _try_load(model_path)
        if not success:
            print(f"[RouterClassifier] {err}，降级到 DeepSeek API")
        return success


def _parse_output(raw: str, original_query: str) -> dict:
    """从模型输出中解析 JSON，失败时返回默认值"""
    raw = raw.strip()

    # 直接尝试解析
    try:
        obj = json.loads(raw)
        intent = obj.get("intent", "amap")
        if intent not in VALID_INTENTS:
            intent = "amap"
        return {
            "intent": intent,
            "query_rewrite": obj.get("query_rewrite", original_query) or original_query,
        }
    except json.JSONDecodeError:
        pass

    # 从文本中提取 JSON 对象
    match = re.search(r'\{[^{}]*"intent"[^{}]*\}', raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            intent = obj.get("intent", "amap")
            if intent not in VALID_INTENTS:
                intent = "amap"
            return {
                "intent": intent,
                "query_rewrite": obj.get("query_rewrite", original_query) or original_query,
            }
        except json.JSONDecodeError:
            pass

    # 关键词回退
    raw_lower = raw.lower()
    if "weather" in raw_lower or "天气" in raw:
        intent = "weather"
    elif "both" in raw_lower or "口碑" in raw:
        intent = "both"
    elif "rag" in raw_lower or "攻略" in raw or "游记" in raw:
        intent = "rag"
    else:
        intent = "amap"

    return {"intent": intent, "query_rewrite": original_query}


def classify(query: str, city: str, model_path: str) -> dict:
    """
    对单条查询做意图分类。

    返回：{"intent": str, "query_rewrite": str}
    失败时返回 None（调用方应降级到 DeepSeek API）
    """
    if not ensure_loaded(model_path):
        return None

    try:
        import torch

        user_content = f'用户查询: "{query}"\n目的地城市: {city}'

        messages = [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        text = _tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = _tokenizer(text, return_tensors="pt").to(_model.device)

        with torch.no_grad():
            output_ids = _model.generate(
                **inputs,
                max_new_tokens=80,
                do_sample=False,        # 贪婪解码，temperature 在此无效，不传
                pad_token_id=_tokenizer.pad_token_id,
            )

        # 只取新生成的 token
        new_ids = output_ids[0][inputs["input_ids"].shape[1]:]
        raw_output = _tokenizer.decode(new_ids, skip_special_tokens=True)

        result = _parse_output(raw_output, query)
        print(f"[RouterClassifier] 本地分类: {result['intent']} | {result['query_rewrite'][:40]}")
        return result

    except Exception as exc:
        print(f"[RouterClassifier] 推理失败，降级: {exc}")
        return None
