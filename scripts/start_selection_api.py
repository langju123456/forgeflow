"""Use the tested task-owned launcher for the fixed V2 application module."""

import start_grounded_api as launcher

from forgeflow.grounded_selection import SELECTION_REVISION

BASE_HEALTH = launcher.health
launcher.MODULE = "forgeflow.grounded_selection_api"


def selection_health():
    import httpx

    base = BASE_HEALTH()
    if base is None:
        return None
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get(launcher.URL + "/grounded/selection-health")
            data = response.json()
        if (
            response.status_code == 200 and type(data) is dict
            and data.get("prompt_revision") == SELECTION_REVISION
            and data.get("process_id") == base["process_id"]
            and data.get("identity") == "DEV_ONLY" and data.get("automatic_business_actions") is False
        ):
            return base
    except (httpx.HTTPError, ValueError):
        pass
    return None


launcher.health = selection_health

if __name__ == "__main__":
    launcher.main()
