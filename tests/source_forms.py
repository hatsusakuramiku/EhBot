"""Read source versions explicitly when preparing ordinary non-racing forms."""
import json
from uuid import uuid4


def batch_versions(client):
    rows = client.get("/api/v1/settings/sources").json()["sources"]
    return {"idempotency_key": str(uuid4()),
            "expected_revisions": json.dumps({str(row["source_id"]): row["revision"] for row in rows})}
