from h03_extra_trap import apply_blank
from h03_queue_blank import project_surfaces

def expose_list(rows: list) -> list:
    return [project_surfaces(apply_blank(dict(it), "list")) for it in rows]
