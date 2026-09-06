"""Export only already-authorized provider configuration to the private SSH directory."""
import json
from pathlib import Path
from experience import read_env, ENV_FILE

def main():
    values = read_env(ENV_FILE)
    keys = ('QWEN_API_KEY', 'QWEN_API_URL', 'TRIP_UNDERSTANDING_QWEN_MODEL',
            'TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION', 'TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION',
            'AMAP_API_KEY', 'DEEPSEEK_API_KEY', 'DEEPSEEK_API_URL', 'OPENAI_API_KEY', 'OPENAI_API_URL',
            'LLM_MODEL_ROUTER', 'LLM_MODEL_SYNTHESIZER', 'NEXT_PUBLIC_AMAP_KEY', 'NEXT_PUBLIC_AMAP_SECURITY_CODE')
    target = Path('D:/CODEX/BreezeTravel-server-access-private-20260906/private-provider.json')
    assert target.parent.is_dir()
    target.write_text(json.dumps({k: values[k] for k in keys if values.get(k)}))
    print('Existing provider config exported privately; no account or application data exported.')

if __name__ == '__main__':
    main()
