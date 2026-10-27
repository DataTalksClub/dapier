"""Read verbatim provider values from the DataOps team secrets store."""
import os
from functools import lru_cache
from .dataops_secrets import SecretReader

@lru_cache
def value(name):
    return SecretReader(table_name=os.environ["TEAM_SECRETS_TABLE"],
                        key_id=os.environ["TEAM_SECRETS_KEY_ID"],
                        region=os.environ["TEAM_SECRETS_REGION"]).read(name)["value"]
