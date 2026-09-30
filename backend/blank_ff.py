def blank_value(_v):
    return None

def blank_list_item(item: dict) -> None:
    item["fill_factor"] = blank_value(item.get("fill_factor"))

def should_blank_path(path: str) -> bool:
    return path in {"list", "create"}
