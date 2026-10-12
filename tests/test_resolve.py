import pytest

from dsdl.resolve import CODE, DATASETS, Target, classify_link, classify_token, match_keys, resolve
from dsdl.sheet import Cell, Row


@pytest.mark.parametrize(
    "url,key,extra",
    [
        ("https://openneuro.org/datasets/ds004196/versions/2.0.2", "openneuro:ds004196", {"version": "2.0.2"}),
        ("https://ds003745", "openneuro:ds003745", {}),
        ("https://osf.io/agre8/overview", "osf:agre8", {}),
        ("https://osf.io/crwz7/wiki?wiki=7y24k", "osf:crwz7", {}),
        ("https://osf.io/qzwsv/files/nupyw", "osf:nupyw", {}),
        ("https://osf.io/abc12/files/osfstorage", "osf:abc12", {}),
        ("https://osf.io/b68fe/?view_only=deadbeef", "osf:b68fe", {"view_only": "deadbeef"}),
        ("https://zenodo.org/records/8208839", "zenodo:8208839", {}),
        ("https://doi.org/10.5281/zenodo.123456", "zenodo:123456", {}),
        ("https://figshare.com/articles/dataset/Mind_captioning_x/25808179", "figshare:25808179", {}),
        ("https://huggingface.co/datasets/FOMO-MRI/FOMO300K", "hf:datasets/FOMO-MRI/FOMO300K", {"kind": "datasets"}),
        ("https://huggingface.co/BrainDiT/BrainDiT/tree/main", "hf:models/BrainDiT/BrainDiT", {"kind": "models"}),
        ("https://gin.g-node.org/gallantlab/shortclips", "gin:gallantlab/shortclips", {}),
        ("https://github.com/HuthLab/deep-fMRI-dataset/tree/1.0.2", "github:HuthLab/deep-fMRI-dataset", {}),
        ("http://github.com/jpeelle/GBUDOT", "github:jpeelle/GBUDOT", {}),
        ("https://htmlpreview.github.io/?https://github.com/YaleMRRC/Node-Parcellation/blob/master/x.html",
         "github:YaleMRRC/Node-Parcellation", {}),
        ("https://www.google.com/url?q=https://zenodo.org/records/5&sa=D", "zenodo:5", {}),
    ],
)
def test_classify_link(url, key, extra):
    t = classify_link(url)
    assert isinstance(t, Target), t
    assert t.key == key
    assert t.extra == extra


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/orgs/NILAB-UvA",
        "https://github.com/MidnightScanClub",
        "https://www.synapse.org/Synapse:syn51156910",
        "https://doi.org/10.57760/sciencedb.27580",
        "https://arxiv.org/abs/2606.20696",
        "https://blclab.org/reading-brain/",
    ],
)
def test_classify_link_manual(url):
    assert isinstance(classify_link(url), str)


def test_classify_token():
    assert classify_token("ds000114").key == "openneuro:ds000114"
    assert classify_token("7884627.0").key == "zenodo:7884627"
    assert classify_token("du4rb").key == "osf:du4rb"
    assert classify_token("HuthLab/deep-fMRI-dataset").key == "github:HuthLab/deep-fMRI-dataset"
    for tok in ("NNDb", "HCP", "Blip2", "Wen", "CNeuroMod", "syn51156910", ""):
        assert classify_token(tok) is None


def _row(tab, n, **cols):
    return Row(tab, n, {k.lower(): (v if isinstance(v, Cell) else Cell(v)) for k, v in cols.items()})


def test_resolve_routes_dedupes_and_reports_manual():
    ds = [
        _row("Datasets", 2, Title="Bimodal", ID=Cell("ds004196\nds006033", [
            "https://openneuro.org/datasets/ds004196/versions/2.0.2",
            "https://openneuro.org/datasets/ds006033/versions/1.0.0"]),
            Code=Cell("GitHub", ["https://github.com/LTU/Inner"])),
        # token-only ID, plus a GitHub token that also appears in the Code column
        _row("Datasets", 3, Title="ANC", ID=Cell("du4rb | HuthLab/x"), Code=Cell("GitHub", ["https://github.com/HuthLab/x"])),
        # same dataset again with a different version → one item, two refs
        _row("Datasets", 4, Title="Bimodal again", ID=Cell("ds004196", ["https://openneuro.org/datasets/ds004196/versions/1.0.0"])),
        # GIN repo in both ID and Code column → only downloaded as a dataset
        _row("Datasets", 5, Title="shortclips", ID=Cell("shortclips", ["https://gin.g-node.org/gallantlab/shortclips"]),
             Code=Cell("Code", ["https://gin.g-node.org/gallantlab/shortclips"])),
        _row("Datasets", 6, Title="HCP", ID=Cell("HCP", ["https://search.kg.ebrains.eu/instances/x"])),
    ]
    code = [
        _row("Code Examples", 2, Name=Cell("SemDec", ["https://github.com/huthlab/X"])),  # case-insensitive dupe
        _row("Code Examples", 3, Name="MIRAGE"),
        _row("Code Examples", 4, Name="Swin"),
        _row("Code Examples", 5, Name=Cell("Paper", ["https://arxiv.org/abs/1"])),
    ]
    res = resolve({"Datasets": ds, "Code Examples": code}, "Datasets", "Code Examples")
    dkeys = [i.key for i in res.for_task(DATASETS)]
    ckeys = [i.key.lower() for i in res.for_task(CODE)]
    assert dkeys == ["openneuro:ds004196", "openneuro:ds006033", "osf:du4rb", "gin:gallantlab/shortclips"]
    assert sorted(ckeys) == ["github:huthlab/x", "github:ltu/inner"]
    bimodal = next(i for i in res.items if i.key == "openneuro:ds004196")
    assert [r.row for r in bimodal.refs] == [2, 4]
    huth = next(i for i in res.items if i.key.lower() == "github:huthlab/x")
    assert {r.tab for r in huth.refs} == {"Datasets", "Code Examples"}
    manual_titles = sorted(m.title for m in res.manual)
    assert manual_titles == ["HCP", "MIRAGE", "Paper", "Swin"]


def test_match_keys():
    res = resolve({"D": [_row("D", 2, Title="t", ID=Cell("ds000001 ds000002"))], "C": []}, "D", "C")
    items = res.items
    assert [i.key for i in match_keys(items, ["DS000001"])] == ["openneuro:ds000001"]
    assert [i.key for i in match_keys(items, ["openneuro:*"])] == ["openneuro:ds000001", "openneuro:ds000002"]
