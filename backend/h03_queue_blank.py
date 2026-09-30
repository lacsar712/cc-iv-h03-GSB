QUEUE_BLANK = True
CARD_BLANK = True

def project_surfaces(row: dict) -> dict:
    out = dict(row)
    if QUEUE_BLANK:
        out["fill_factor"] = None
    if CARD_BLANK and out.get("fill_factor") is None:
        out["ff_display"] = ""
    return out
