"""
Sprint 3 — F1：Qwen2.5-1.5B LoRA 微调 Router 意图分类器

硬件要求：RTX 4060 Laptop（8GB VRAM），fp16 训练
基础模型：Qwen/Qwen2.5-1.5B-Instruct（Hugging Face）

训练策略：
  - 任务：指令微调（Instruction Following），让模型给定查询 → 输出 JSON 意图标签
  - 方法：SFTTrainer（TRL）+ LoRA（PEFT）
  - 精度：fp16（混合精度），8GB 显存可跑
  - LoRA：r=16, alpha=32，target_modules q/k/v/o_proj

用法：
  pip install -r requirements_finetune.txt
  python -m scripts.train_router \
    --train data/router_train.jsonl \
    --output models/router_lora \
    --epochs 3

训练完成后，设置 .env：
  FT_ROUTER_ENABLED=true
  FT_ROUTER_MODEL_PATH=backend/models/router_lora
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=False)
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

import torch


def check_gpu():
    if not torch.cuda.is_available():
        print("警告：未检测到 GPU，将使用 CPU 训练（速度较慢）")
        return "cpu"
    gpu_name = torch.cuda.get_device_name(0)
    vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"GPU: {gpu_name} ({vram_gb:.1f} GB VRAM)")
    return "cuda"


def load_dataset_from_jsonl(path: str):
    """加载 JSONL 数据，返回 HuggingFace Dataset"""
    from datasets import Dataset

    samples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                # 只保留 messages 字段（SFTTrainer 期望的格式）
                samples.append({"messages": obj["messages"]})

    print(f"加载数据: {len(samples)} 条 from {path}")
    return Dataset.from_list(samples)


def build_lora_config():
    from peft import LoraConfig, TaskType

    return LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,                           # LoRA 秩
        lora_alpha=32,                  # scaling = alpha/r = 2
        lora_dropout=0.05,
        bias="none",
        target_modules=[                # Qwen2.5 的 attention 投影层
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",  # MLP 层（可选，提升效果但增显存）
        ],
        # 只训练 attention 层以节省显存可注释掉 MLP 行
    )


def build_sft_config(output_dir: str, epochs: int, device: str, max_length: int):
    from trl import SFTConfig

    use_fp16 = device == "cuda"

    return SFTConfig(
        output_dir=output_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=1,      # 降为 1，减少每步显存带宽 → 抑制热降频
        gradient_accumulation_steps=16,     # 等效 batch_size=16，梯度不变
        warmup_steps=10,
        learning_rate=2e-4,
        fp16=use_fp16,
        bf16=False,
        logging_steps=20,
        save_strategy="epoch",
        save_total_limit=2,
        eval_strategy="no",
        report_to="none",
        dataloader_num_workers=0,           # Windows 兼容
        remove_unused_columns=False,
        optim="adamw_torch",
        lr_scheduler_type="cosine",
        weight_decay=0.01,
        max_grad_norm=1.0,
        max_length=max_length,              # 由 --max-length 参数控制（默认 256）
        gradient_checkpointing=True,        # 用重计算换显存，降低峰值功耗
    )


def main():
    parser = argparse.ArgumentParser(description="Qwen2.5-1.5B LoRA 微调 Router 分类器")
    parser.add_argument("--train", default="data/router_train.jsonl", help="训练数据路径")
    parser.add_argument("--output", default="models/router_lora", help="LoRA 适配器输出目录")
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-1.5B-Instruct", help="基础模型（HF Hub 或本地路径）")
    parser.add_argument("--epochs", type=int, default=3, help="训练轮数")
    parser.add_argument("--max-length", type=int, default=256, help="最大 token 长度（实际序列最长 ~193，256 足够）")
    args = parser.parse_args()

    if not Path(args.train).exists():
        print(f"错误：训练数据不存在: {args.train}", file=sys.stderr)
        print("请先运行: python -m scripts.generate_training_data", file=sys.stderr)
        sys.exit(1)

    device = check_gpu()

    # ── 导入（延迟到此处，避免无 GPU 时也强制加载 torch）─────────────────────
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import get_peft_model
    from trl import SFTTrainer

    print(f"\n加载基础模型: {args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model,
        trust_remote_code=True,
        padding_side="right",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # fp16 加载（8GB 显存下 1.5B 约 3GB，有足够空间训练）
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        device_map="auto" if device == "cuda" else None,
        trust_remote_code=True,
    )

    model.enable_input_require_grads()

    # ── 应用 LoRA ──────────────────────────────────────────────────────────────
    lora_config = build_lora_config()
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── 加载数据 ───────────────────────────────────────────────────────────────
    train_dataset = load_dataset_from_jsonl(args.train)

    # ── 训练参数（TRL 1.x: SFTConfig 包含 max_seq_length）──────────────────────
    sft_config = build_sft_config(args.output, args.epochs, device, args.max_length)

    # ── SFTTrainer（TRL 1.x: tokenizer → processing_class）────────────────────
    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=train_dataset,
        processing_class=tokenizer,
    )

    # ── 开始训练 ───────────────────────────────────────────────────────────────
    print(f"\n开始训练：{args.epochs} epochs，输出目录: {args.output}")
    trainer.train()

    # ── 保存 LoRA 适配器 ────────────────────────────────────────────────────────
    output_path = Path(args.output)
    trainer.model.save_pretrained(str(output_path))
    tokenizer.save_pretrained(str(output_path))

    # 写入元数据，供推理时读取基础模型路径
    meta = {
        "base_model": args.base_model,
        "lora_r": 16,
        "lora_alpha": 32,
        "task": "router_intent_classification",
        "intents": ["amap", "rag", "both", "weather"],
    }
    (output_path / "adapter_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )

    print(f"\nLoRA 适配器已保存到: {output_path}")
    print("下一步：在 .env 中设置 FT_ROUTER_ENABLED=true 和 FT_ROUTER_MODEL_PATH=backend/models/router_lora")


if __name__ == "__main__":
    main()
