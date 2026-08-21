from typing import Dict
from fastapi import Request

def get_user_from_headers(request: Request) -> Dict[str, str]:
    """
    Extracts Authentik ForwardAuth trusted identity headers.
    Never logs these values.
    """
    return {
        "username": request.headers.get("x-authentik-username", ""),
        "email": request.headers.get("x-authentik-email", ""),
        "name": request.headers.get("x-authentik-name", ""),
        "uid": request.headers.get("x-authentik-uid", ""),
        "groups": request.headers.get("x-authentik-groups", ""),
    }
