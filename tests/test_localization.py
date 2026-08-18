import json

from palworld_console.localization import GameLocalizationService


def test_localization_priority_and_unknown_fallback(tmp_path):
    service = GameLocalizationService(tmp_path)
    assert service.display("pals", "SheepBall") == "棉悠悠"
    assert service.display("items", "Unknown_Item") == "未知物品（Unknown_Item）"
    service.save_overrides({"pals": {"SheepBall": "自定义棉悠悠"}})
    assert service.display("pals", "SheepBall") == "自定义棉悠悠"


def test_imported_catalog_overrides_builtin_without_changing_id(tmp_path):
    source = tmp_path / "catalog.json"
    source.write_text(json.dumps({"build_id": "test-build", "entries": {"pals": {"SheepBall": "资源版名称"}, "items": {"NewItem": "新物品"}}}, ensure_ascii=False), encoding="utf-8")
    service = GameLocalizationService(tmp_path / "app")
    catalog = service.import_catalog(source)
    value = service.resolve("pals", "SheepBall")
    assert catalog.build_id == "test-build"
    assert value.key == "SheepBall" and value.text == "资源版名称" and value.source == "game"
    assert service.display("items", "NewItem") == "新物品"


def test_import_common_pal_and_item_asset_layout(tmp_path):
    assets = tmp_path / "assets"; assets.mkdir()
    (assets / "pal.json").write_text(json.dumps({"zh": {"SheepBall": "资源棉悠悠", "NewPal": "新帕鲁"}}, ensure_ascii=False), encoding="utf-8")
    (assets / "items.json").write_text(json.dumps({"zh": [{"key": "PalSphere", "name": "资源帕鲁球"}, {"id": "new_item", "name": "新道具"}]}, ensure_ascii=False), encoding="utf-8")
    service = GameLocalizationService(tmp_path / "app")
    catalog = service.import_asset_directory(assets)
    assert catalog.entries["pals"]["NewPal"] == "新帕鲁"
    assert service.display("pals", "SheepBall") == "资源棉悠悠"
    assert service.display("items", "new_item") == "新道具"
