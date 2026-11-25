from pathlib import Path

from qa_platform.runner.page_index import build_page_index, fill_route


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _product(root: Path) -> None:
    _write(
        root / "app/YouZanBillController.java",
        '@RequestMapping("/bill/youZan")\npublic class YouZanBillController {}\n',
    )
    _write(
        root / "app/YouZanBillEntity.java",
        '@Table("bfrs_you_zan_bill")\npublic class YouZanBillEntity {}\n',
    )
    _write(
        root / "app/KuaiShopGoodsBillController.java",
        '@RequestMapping("Kuai-shop-goods-bill")\npublic class KuaiShopGoodsBillController {}\n',
    )
    _write(
        root / "app/KuaiShopGoodsBillEntity.java",
        '@Table("bfrs_kuai_shop_goods_bill")\npublic class KuaiShopGoodsBillEntity {}\n',
    )
    _write(
        root / "app/VipBillController.java",
        '@RequestMapping("/bill/vip")\npublic class VipBillController {}\n',
    )
    _write(
        root / "app/VipBillEntity.java",
        '@Table("bfrs_vip_bill")\npublic class VipBillEntity {}\n',
    )
    _write(
        root / "app/ShopifyBillController.java",
        '@RequestMapping("/bill/shopify")\npublic class ShopifyBillController {}\n',
    )
    _write(
        root / "app/ShopifyBillEntity.java",
        '@Table("bfrs_shopify_bill")\npublic class ShopifyBillEntity {}\n',
    )
    _write(
        root / "app/DupLeftController.java",
        '@RequestMapping("/bill/dup")\npublic class DupLeftController {}\n',
    )
    _write(
        root / "app/DupLeftEntity.java",
        '@Table("bfrs_dup_left")\npublic class DupLeftEntity {}\n',
    )
    _write(
        root / "app/DupRightController.java",
        '@RequestMapping("/bill/dup")\npublic class DupRightController {}\n',
    )
    _write(
        root / "app/DupRightEntity.java",
        '@Table("bfrs_dup_right")\npublic class DupRightEntity {}\n',
    )


def _front(front: Path) -> None:
    _write(
        front / "src/config/route.js",
        "{ path: '/bill/platformBill', component: 'bill/platform-bill' }\n"
        "{ path: '/other', component: 'other/page' }\n",
    )
    _write(
        front / "src/views/bill/platform-bill/index.vue",
        "import YouZan from '@/views/bill/platform-bill/youzan/index.vue';\n"
        "import KuaiPayment from '@/views/bill/platform-bill/ks-payment/index.vue';\n"
        "import Vip from '@/views/bill/platform-bill/vip/index.vue';\n"
        "import Dup from '@/views/bill/platform-bill/dup/index.vue';\n"
        "{ label: '有赞账单', component: YouZan },\n"
        "{ label: '快手货款账单', component: KuaiPayment },\n"
        "{ label: '重复账单', component: Dup },\n"
        "<template>\n"
        "  <el-tab-pane label=\"唯品会账单\" name=\"13\">\n"
        "    <vip></vip>\n"
        "  </el-tab-pane>\n"
        "</template>\n",
    )
    _write(
        front / "src/views/bill/platform-bill/youzan/index.vue",
        "import { YouZanBillApi } from '@/api/bill/platform-bill/youzan';\n"
        "useList({ api: YouZanBillApi });\n",
    )
    _write(
        front / "src/api/bill/platform-bill/youzan/index.ts",
        "static get baseUrl() {\n  return '/bill/youZan';\n}\n",
    )
    _write(
        front / "src/views/bill/platform-bill/youzan/attrs/filter-bar-attrs.ts",
        "label: '资金账户',\nfieldName: 'fundingAccountIds',\n"
        "label: '交易号',\nfieldName: 'tradeIds',\n",
    )
    _write(
        front / "src/views/bill/platform-bill/ks-payment/index.vue",
        "import { KuaiShopGoodsBillApi } from '@/api/bill/platform-bill/ks-payment';\n"
        "useList({ api: KuaiShopGoodsBillApi });\n",
    )
    _write(
        front / "src/api/bill/platform-bill/ks-payment/index.ts",
        "return '/Kuai-shop-goods-bill';\n",
    )
    _write(
        front / "src/views/bill/platform-bill/vip/index.vue",
        "import { VipBillApi } from '@/api/bill/platform-bill/vip';\n"
        "useList({ api: VipBillApi });\n",
    )
    _write(
        front / "src/api/bill/platform-bill/vip/index.ts",
        "static get baseUrl() {\n  return '/bill/vip';\n}\n"
        "return request.post(this.baseUrl + '/reParse', params);\n",
    )
    _write(
        front / "src/views/bill/platform-bill/dup/index.vue",
        "import { DupApi } from '@/api/bill/platform-bill/dup';\n"
        "useList({ api: DupApi });\n",
    )
    _write(
        front / "src/api/bill/platform-bill/dup/index.ts",
        "return '/bill/dup';\n",
    )


def test_index_joins_tabs_to_controllers_outside_the_commit(tmp_path):
    repo = tmp_path / "bfrs"
    front = tmp_path / "bfrs-front"
    _product(repo)
    _front(front)

    rows = {row["tab"]: row for row in build_page_index(str(repo))}

    assert rows["有赞账单"]["class_name"] == "YouZanBillController"
    assert rows["有赞账单"]["table"] == "bfrs_you_zan_bill"
    assert rows["有赞账单"]["mapping"] == "/bill/youZan"
    assert rows["有赞账单"]["route"] == "/bill/platformBill"
    assert rows["有赞账单"]["filters"] == ["资金账户", "交易号"]
    assert rows["有赞账单"]["source"].startswith("app/YouZanBillController.java:")
    assert rows["快手货款账单"]["mapping"] == "/Kuai-shop-goods-bill"
    assert rows["快手货款账单"]["class_name"] == "KuaiShopGoodsBillController"
    assert rows["唯品会账单"]["mapping"] == "/bill/vip"
    assert "Shopify" not in {row["class_name"] for row in rows.values()}
    assert "重复账单" not in rows

    assert build_page_index(str(repo), str(tmp_path / "missing")) == []
    custom = tmp_path / "custom-front"
    _front(custom)
    renamed = tmp_path / "service"
    _product(renamed)
    custom_rows = build_page_index(str(renamed), str(custom))
    assert {row["tab"] for row in custom_rows} == {"有赞账单", "快手货款账单", "唯品会账单"}


def test_fill_route_uses_one_front_route_and_keeps_an_existing_path():
    pages = [
        {"tab": "有赞账单", "route": "/bill/platformBill"},
        {"tab": "唯品会账单", "route": "/bill/platformBill"},
    ]
    plan = {"ui": {"pages": [{"title_text": "结算页", "path": ""}], "surfaces": ["有赞账单", "唯品会账单"]}}
    assert fill_route(plan, pages) == "/bill/platformBill"
    assert plan["ui"]["pages"][0]["path"] == "/bill/platformBill"

    kept = {"ui": {"pages": [{"title_text": "结算页", "path": "/already"}], "surfaces": ["有赞账单"]}}
    assert fill_route(kept, pages) == ""
    assert kept["ui"]["pages"][0]["path"] == "/already"

    split = {"ui": {"pages": [{"title_text": "结算页", "path": ""}], "surfaces": ["有赞账单", "另一页"]}}
    other = pages + [{"tab": "另一页", "route": "/other"}]
    assert fill_route(split, other) == ""
    assert split["ui"]["pages"][0]["path"] == ""
