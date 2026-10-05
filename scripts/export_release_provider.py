"""Export only already-authorized provider configuration to the private SSH directory."""
import json
from pathlib import Path
from experience import read_env, ENV_FILE

def main():
    values = read_env(ENV_FILE)
    keys = ('KIMI_FOR_CODE', 'QWEN_API_KEY', 'TRIP_SEMANTIC_API_KEY',
            'TRIP_SEMANTIC_PROVIDER', 'TRIP_SEMANTIC_BASE_URL', 'TRIP_SEMANTIC_MODEL',
            'TRIP_SEMANTIC_CREDENTIAL_REF', 'TRIP_SEMANTIC_REASONING_EFFORT', 'TRIP_SEMANTIC_OUTPUT_MODE',
            'TRIP_SEMANTIC_DEADLINE_SECONDS', 'TRIP_SEMANTIC_MAX_OUTPUT_TOKENS',
            'TRIP_SEMANTIC_MAX_CALLS', 'TRIP_SEMANTIC_TOTAL_SECONDS', 'TRIP_SEMANTIC_LEGACY_CONFIG')
    target = Path('D:/CODEX/BreezeTravel-server-access-private-20260906/private-provider.json')
    assert target.parent.is_dir()
    target.write_text(json.dumps({k: values[k] for k in keys if values.get(k)}))
    print('Existing provider config exported privately; no account or application data exported.')

if __name__ == '__main__':
    main()
