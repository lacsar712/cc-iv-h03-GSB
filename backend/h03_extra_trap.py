from blank_ff import blank_list_item, should_blank_path

def apply_blank(item: dict, path: str) -> dict:
    if not should_blank_path(path):
        return item
    out = dict(item)
    blank_list_item(out)
    return out
