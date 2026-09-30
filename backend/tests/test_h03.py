from h03_extra_trap import apply_blank
from h03_map_trap import expose_list

def test_blank():
    d = {"fill_factor": 0.78}
    apply_blank(d, "list")
    assert d["fill_factor"] is None
    rows = expose_list([{"fill_factor": 0.78}])
    assert rows[0]["fill_factor"] in (0, None)
