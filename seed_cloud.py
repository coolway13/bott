"""Export allowlisted existing game state for one-time cloud migration.
Stop the Mac worker first. The export has no webhook or API keys.
"""
import gzip
import json
from pathlib import Path
import cloud
import local

if __name__=='__main__':
    with local.connect() as db:
        cloud.schema(db)
        payload=json.dumps(cloud.snapshot(db)).encode()
    target=Path('data/state.json.gz')
    target.write_bytes(gzip.compress(payload,mtime=0))
    print('Wrote data/state.json.gz. Import on bot-state branch before first run.')
