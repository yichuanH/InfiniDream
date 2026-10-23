"""infinigen 原生 scatter 系統的目錄 —— ★唯一來源★。

★為什麼要這一份★(2026-09-07)
  0907 的 alien forest case 想撒一片發光藍花，director 卻只能標成
  `feasible: false`：ground_scatter 的 add 被 ACTION_SUPPORT 一律判死，理由是
  「scatter 是 compose_nature(coarse) 建的」。但 apply_plan 早就有 op="create"，
  註解白紙黑字寫著「可以在現成 coarse blend 上憑空長出一整個 scatter 系統，
  不用重跑 coarse」—— ★不可行是路由政策，不是技術限制★。
  真正缺的只有三樣：一份能選的系統清單、selection(撒在哪)、以及把外觀換成
  我們生成的 factory 的掛勾。這支補齊第一樣，另外兩樣由 apply_plan 消費。

★為什麼不走 hero placeholder★
  一朵一朵 placeholder 要 populate 60 個資產，而且同 factory 自碰撞會被
  placer 的 same_factory_collision_review 全數擋下(0904 實測 137 個 hard
  failure)。GN scatter 的位置★根本不存在於 blend 裡★(render 時才算)，
  所以沒有個體可碰撞，密度也不用跟容量契約打架。

★外觀替換的掛勾★
  26 個 scatter 模組都是在 apply() 內部用★模組全域名稱★查 factory：
      flowerplant.py:  FlowerPlantFactory(np.random.randint(1e5))
      grass.py:        GrassTuftFactory(np.random.randint(1e7))
  ⇒ 生成端產出 overlay factory 後，setattr(module, factory_symbol, cls) 就能
    整批換掉母本外觀，26 個系統一體適用，不必逐支特製。
  ⚠️ 只有 5 支把母本存成 self.col(Mushrooms/Lichen/MossCover/Ivy/ClothesCover)，
     其餘 21 支在 apply() 內部建 collection ⇒ ★不能用換 self.col 的老路★。
  ⚠️ ★factory_symbol 不是「符號存在就能換」★ —— 還要看它★被怎麼呼叫★。
     我們生成的 overlay factory 一律是 (factory_seed, coarse=False) 簽章，
     所以只有「用單一 seed 位置參數建構」的系統可以安全替換。實測不相容的：
         GenericTreeFactory(seed, season, **5 kwargs)   chopped_trees
         MonocotFactory(seed, grass=...)                monocots
         BlenderRockFactory(seed, detail=...)           pebbles
         LeafFactoryIvy(seed, random_season())          ivy
     這些一律標 factory_symbol=None ⇒ 用官方外觀，而不是在 step 8 才 TypeError。
"""
from __future__ import annotations

import re

# ── 地形 domain → infinigen 的 tag 字串 ────────────────────────────────────
#   ★照抄 configs_nature/base_nature.gin:8-10★，不要自己編。
#   tag_mask 是用 "." 切開比對關鍵字，前綴 "-" 是排除。
DOMAIN_TAGS = {
    "land":       "landscape,-liquid_covered,-cave,-beach",
    "nonliving":  "landscape,-cave",
    "underwater": "landscape,liquid_covered,-cave",
    "cave":       "landscape,cave",
    "beach":      "landscape,beach",
}
DEFAULT_DOMAIN = "land"

# ★鏡頭可見性★(2026-09-07)
#   terrain 會把地形切成 in-view 與 out_of_view 兩組 MaskTag
#   ("landscape.out_of_view.terrain" 等)。撒地被時★預設排除畫面外★：
#     - 畫面外的花海對成圖沒有貢獻，卻要吃 render 時的 instancing 成本
#     - 而且 base_obj 有時是整片 OpaqueTerrain(沒有 .inview_near 可用)，
#       這時 tag 是唯一能把範圍收回鏡頭前的手段
#   ⚠️ 這是「排除已知在畫面外的」，不是「保證在畫面內」——tag 只有兩種值，
#      沒有更細的視錐資訊。對整片地被來說這個精度剛好夠。
CAMERA_VISIBLE_EXCLUDE = "-out_of_view"

# ── scatter 系統目錄 ───────────────────────────────────────────────────────
#   module          infinigen.assets.scatters.<module>
#   cls             類別名(None = 只有模組層級的 apply 函式)
#   factory_symbol  ★換外觀時要 patch 的模組全域名★(None = 不支援換外觀)
#   domain          預設撒在哪個地形 domain
#   surfaces        這個系統在哪些 surface_registry 類型上讀得通(給 director 選)
#   density         apply() 的預設密度；None = 該系統沒有 density 參數
SCATTERS = {
    # ── 陸地植被 ──
    "grass":             dict(module="grass",             cls="Grass",
                              factory_symbol="GrassTuftFactory",
                              domain="land", surfaces=("grass", "soil"),
                              density=None,
                              mask_scale=0.1,
                              select_thresh=0.375,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="inview",
                              note="草叢。density 由 vol_density 內部決定"),
    "flowers":           dict(module="flowerplant",       cls="Flowerplant",
                              factory_symbol="FlowerPlantFactory",
                              domain="land", surfaces=("grass", "soil"),
                              density_semantics="clipped",
                              density=1.0,
                              # ★自己的值域★：這一支是 clip(density/avg_vol, 0, 200)，
                              #   母本小的時候倍率加大會撞上 200/m² 的天花板而沒有效果
                              #   (0907 實測 density=4.0 對當時的小母本就已經頂死)。
                              #   所以不用共用倍率，改用一組收斂一點的絕對值。
                              density_levels={"sparse": 0.5, "normal": 1.0,
                                              "dense": 2.0, "very_dense": 3.0},
                              mask_scale=0.01,
                              select_thresh=0.6,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="inview",
                              # ⚠️ note 會★原樣進 director 的選單★，所以必須是
                              #    中性描述。這裡原本寫「本 case 要的就是這支」——
                              #    那是某一次測試的答案洩漏進通用選單，等於在
                              #    ★每一個場景★都對 flowers 加分(2026-09-09 移除)。
                              note="整片開花植株(花海)"),
    "monocots":          dict(module="monocots",          cls="Monocots",
                              factory_symbol=None,   # MonocotFactory(seed, grass=...)
                              domain="land", surfaces=("grass", "soil", "sand"),
                              density=None, mask_scale=0.2,
                              select_thresh=0.55,
                              normal_thresh=None,
                              return_scalar=False,
                              terrain_target="inview",
                              note="單子葉叢生植物(蘆葦/劍麻類)"),
    "ferns":             dict(module="fern",              cls="Fern",
                              factory_symbol="FernFactory",
                              domain="land", surfaces=("soil", "grass"),
                              density_semantics="per_volume",
                              density=("uniform", 1, 6), mask_scale=0.1,
                              select_thresh=0.6,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="inview",
                              note="蕨類"),
    "decorative_plants": dict(module="decorative_plants", cls="DecorativePlants",
                              factory_symbol=None,
                              domain="land", surfaces=("soil", "grass"),
                              density=None, mask_scale=0.125,
                              select_thresh=0.575,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="near",
                              note="觀賞植物混合"),
    "ground_leaves":     dict(module="ground_leaves",     cls="GroundLeaves",
                              factory_symbol=None,
                              domain="land", surfaces=("soil", "grass"),
                              density_semantics="per_area",
                              density=70, mask_scale=0.1,
                              select_thresh=0.52,
                              normal_thresh=0.7,
                              return_scalar=True,
                              terrain_target="near",
                              note="地面落葉"),
    "ground_twigs":      dict(module="ground_twigs",      cls="GroundTwigs",
                              factory_symbol=None,
                              domain="land", surfaces=("soil", "grass"),
                              density=None, mask_scale=0.15,
                              select_thresh=0.55,
                              normal_thresh=0.7,
                              return_scalar=True,
                              terrain_target="near",
                              note="地面枯枝"),
    "mushroom":          dict(module="ground_mushroom",   cls="Mushrooms",
                              factory_symbol="MushroomFactory",
                              domain="land", surfaces=("soil", "grass"),
                              density_semantics="per_area",
                              density=2.0, mask_scale=0.1,
                              select_thresh=0.65,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="near",
                              note="蘑菇"),
    "pinecone":          dict(module="pinecone",          cls="Pinecone",
                              factory_symbol="PineconeFactory",
                              domain="land", surfaces=("soil", "grass"),
                              density=None, mask_scale=0.1,
                              select_thresh=0.63,
                              normal_thresh=None,
                              return_scalar=False,
                              terrain_target="near",
                              note="毬果"),
    "pine_needle":       dict(module="pine_needle",       cls="PineNeedle",
                              factory_symbol="PineNeedleFactory",
                              domain="land", surfaces=("soil", "grass"),
                              # ★density 是死參數★：apply() 收下但函式體完全沒用到，
                              #   覆蓋率固定在 vol_density=U(0.01,0.03)。
                              density=None, mask_scale=0.125,
                              select_thresh=0.475,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="near",
                              note="松針"),
    "chopped_trees":     dict(module="chopped_trees",     cls="ChoppedTrees",
                              factory_symbol=None,   # GenericTreeFactory(seed, season, **5 kwargs)
                              domain="land", surfaces=("soil", "grass"),
                              density=None, mask_scale=0.15,
                              select_thresh=0.575,
                              normal_thresh=0.7,
                              return_scalar=True,
                              terrain_target="inview",
                              note="倒木/伐木堆"),
    # ── 岩石 ──
    "pebbles":           dict(module="pebbles",           cls="Pebbles",
                              factory_symbol=None,   # BlenderRockFactory(seed, detail=...)
                              domain="nonliving", surfaces=("rock", "soil", "sand"),
                              density=None, mask_scale=0.15,
                              select_thresh=0.5,
                              normal_thresh=0.7,
                              return_scalar=True,
                              terrain_target="inview",
                              note="碎石"),
    # ── 附著在物體表面(grime 那一類，base_obj 給 hero 而非地形) ──
    "moss":              dict(module="moss",              cls="MossCover",
                              factory_symbol="MossFactory",
                              domain="land", surfaces=("rock", "soil"),
                              density=None, official_grime=True,
                              note="苔蘚"),
    "lichen":            dict(module="lichen",            cls="Lichen",
                              factory_symbol="LichenFactory",
                              domain="nonliving", surfaces=("rock",),
                              density=None, official_grime=True,
                              note="地衣"),
    "ivy":               dict(module="ivy",               cls="Ivy",
                              factory_symbol=None,
                              domain="land", surfaces=("rock", "soil"),
                              density=None, official_grime=True,
                              note="常春藤"),
    "slime_mold":        dict(module="slime_mold",        cls="SlimeMold",
                              factory_symbol=None,
                              domain="land", surfaces=("soil",),
                              density=None, official_grime=True,
                              note="黏菌"),
    # ── 水下 ──
    "coral_reef":        dict(module="coral_reef",        cls="CoralReef",
                              factory_symbol="CoralFactory",
                              domain="underwater", surfaces=("underwater_ground",),
                              density_semantics="per_area",
                              density=2.5, mask_scale=0.15,
                              select_thresh=0.46,
                              normal_thresh=None,
                              return_scalar=False,
                              terrain_target="inview",
                              note="珊瑚礁"),
    "seaweed":           dict(module="seaweed",           cls="Seaweed",
                              factory_symbol="SeaweedFactory",
                              domain="underwater", surfaces=("underwater_ground",),
                              # ★density 是死參數★：apply() 收下但函式體完全沒用到，
                              #   覆蓋率固定在 vol_density=U(2,10)。傳了不會報錯也不會生效。
                              density=None, mask_scale=0.05,
                              select_thresh=0.5,
                              normal_thresh=0.4,
                              return_scalar=False,
                              terrain_target="inview",
                              note="海藻"),
    "urchin":            dict(module="urchin",            cls="Urchin",
                              factory_symbol="UrchinFactory",
                              domain="underwater", surfaces=("underwater_ground",),
                              density=None, mask_scale=0.05,
                              select_thresh=0.5,
                              normal_thresh=None,
                              return_scalar=False,
                              terrain_target="inview",
                              note="海膽"),
    "seashells":         dict(module="seashells",         cls="Seashells",
                              factory_symbol="MolluskFactory",
                              domain="underwater", surfaces=("underwater_ground", "sand"),
                              density_semantics="per_volume",
                              density=("uniform", 0.2, 1.0), mask_scale=0.05,
                              select_thresh=0.5,
                              normal_thresh=None,
                              return_scalar=True,
                              terrain_target="near",
                              note="貝殼"),
    "mollusk":           dict(module="mollusk",           cls="Mollusk",
                              factory_symbol="MolluskFactory",
                              domain="underwater", surfaces=("underwater_ground", "sand"),
                              density_semantics="per_area",
                              density=1.0, official_unused=True,
                              note="軟體動物"),
    "jellyfish":         dict(module="jellyfish",         cls="Jellyfish",
                              factory_symbol="JellyfishFactory",
                              domain="underwater", surfaces=("underwater_ground",),
                              density_semantics="per_area",
                              density=1.0, mask_scale=0.05,
                              select_thresh=0.5,
                              normal_thresh=None,
                              return_scalar=False,
                              terrain_target="inview",
                              note="水母群(懸浮於水體)"),
}

# ⚠️ snow_layer ★刻意不列★(2026-09-08 更正理由) —— 先前寫「沒有 selection 參數」
#    是錯的。它★有★參數，但★收下之後直接丟掉★：
#        def apply(obj, selection=None):
#            snowlayer = Snowlayer()
#            snowlayer.apply(obj)        # selection 沒有往下傳
#    而 Snowlayer.apply(self, obj, **kwargs) 走 bpy.ops.snow.create()，是整個
#    物件蓋一層雪。★這比「沒有參數」更危險★ —— 傳 selection 不會報錯，只會
#    無聲地蓋滿整片地形。要雪層請走別的路。
UNSUPPORTED = {
    "snow_layer": "apply() 收 selection 但直接丟棄，實際是整個物件蓋滿，無法指定範圍",
}

# ══════════════════════════════════════════════ 密度語意分級(2026-09-08)
#
# ★為什麼是選詞而不是給數字★
#   density 的單位反直覺：官方每一支都是先 density/avg_vol 才變成 株/m²
#   (見 flowerplant.apply)，所以同一個 4.0 對小母本是一道樹籬、對大母本是
#   稀疏幾朵。實測要 LLM 猜這個浮點數的結果是：0907 的 composer 寫 133.312、
#   0908 的同一支直接不寫 —— 同一個欄位兩次跑出兩種極端。
#   ⇒ agent 只做四選一，換算交給這張★確定性的表★。
#
# ★基準是官方預設，不是 1.0★
#   倍率乘的是 SCATTERS[name]["density"]，那一欄現在逐筆核對過官方 apply()
#   的預設值(pine_needle 是 2e3、ground_leaves 是 70、ferns/seashells 是
#   ("uniform", a, b) 的分布)。normal 就等於「官方長什麼樣」。
#
# ★不是每一支都能調★
#   25 支官方 scatter 裡只有 10 支的 apply() 收 density；其餘的覆蓋率旋鈕
#   要嘛不存在(grass 由內部 vol_density=U(0.5,5) 決定)，要嘛是 n(母本數量，
#   意思完全不同)。density=None 的系統 density_for() 一律回 None，
#   ★呼叫端就不要傳 density★，讓它走官方預設。
DENSITY_LEVELS = {
    "sparse":     0.4,
    "normal":     1.0,
    "dense":      2.5,
    "very_dense": 4.0,
}
DEFAULT_DENSITY_LEVEL = "normal"

# ★覆蓋率也跟著語意等級走 —— 但基準是★官方逐支調過的值★(2026-09-09 改)
#   先前用一張固定表(very_dense=0.20)，那是在「我們寫死 0.5」的前提下訂的。
#   實際查 infinigen_examples/generate_nature.py，官方★每一支都各自調過★：
#       flowers 0.6(scale 0.01)  ferns 0.6      mushroom 0.65   pinecone 0.63
#       grass   0.25~0.5         ground_leaves 0.52             pebbles 0.5
#   ⇒ 固定表會把每一支都拉到同一個值，等於抹掉官方的調校。
#   改成★相對位移★：normal = 官方原值(完全不改變行為)，其他等級平移門檻。
#   門檻不是尺度不變量(0.6 的 2 倍沒有意義)，所以用加減而不是乘除。
SELECT_THRESH_OFFSETS = {
    "sparse":     +0.12,
    "normal":      0.00,
    "dense":      -0.15,
    "very_dense": -0.30,
}
SELECT_THRESH_MIN, SELECT_THRESH_MAX = 0.05, 0.95
DEFAULT_SELECT_THRESH = 0.5     # catalog 沒記到官方值時的保守回退

# ★「不明顯」的旋鈕是 coverage，不是 density★(2026-09-09 量測)
#   0909 smoke 的 ferns：52 個相機可見 anchor 蓋住基底 13.94%(≈260m²)，
#   而 fern 的官方有效密度是
#       vol_density / (avg_vol * avg_scale²) = 3.5 / (1.15 * 0.387²) ≈ 20 株/m²
#   ⇒ 應該有數千株，實際只長 2 株。密度不是瓶頸，★遮罩通過率才是★。
#   官方遮罩是 noise(scale=0.1) > N(0.6, 0.025)：scale 0.1 代表特徵約 10m 寬，
#   48×39m 的基底只裝得下約 16 個 noise cell、通過的只有 3~4 塊
#   ⇒ ★相機有沒有對到通過的那一塊純粹靠運氣★。這一條解釋了 0909 清單裡
#   ferns/decorative_plants/pine_needle/flowers/pebbles/seashells 的「看不出來」
#   與「只集中在一塊」。
#
#   所以自動補救要動的是 select_thresh(官方自己的旋鈕、density level 用的同一個)，
#   不是 density。這是★相對於 director 選定等級★再往下疊的安全網，每一階都
#   寫進 report，永遠看得出偏離了官方 normal 值多少。
COVERAGE_ESCALATION_OFFSETS = (0.00, -0.15, -0.30, -0.45)

# 每一支要「看得出來」所需的★畫面內有效實例數★。
#   contract 先前寫死 1 —— 那讓 0909 的 ferns(2 株、effective=1)被判成 PASS，
#   等於用「至少一株」冒充「撒得明顯」。門檻跟母本在畫面上的大小成反比。
MIN_EFFECTIVELY_VISIBLE = {
    # 大型、本來就該稀疏
    "chopped_trees":     6,
    # 中型單株：看得出「有一叢」需要十幾株
    "ferns":            15,
    "decorative_plants": 15,
    "monocots":         15,
    "mushroom":         15,
    "coral_reef":       15,
    "jellyfish":        10,
    "seaweed":          15,
    "mollusk":          15,
    # 小型地被：要成片才讀得出來
    "grass":            40,
    "flowers":          40,
    "ground_leaves":    40,
    "ground_twigs":     30,
    "pine_needle":      40,
    "pebbles":          30,
    "pinecone":         30,
    "seashells":        30,
    "urchin":           30,
}
DEFAULT_MIN_EFFECTIVELY_VISIBLE = 15

# ★官方邊緣淡出的下限★(2026-09-09)
#   placement_mask(return_scalar=True) 的第二個回傳值是
#       MapRange(noise, From_Min=select_thresh, From_Max=0.75, SMOOTHSTEP)
#   scatter_instances 拿它乘上★尺寸★(taper_scale：flowerplant/grass/
#   ground_mushroom)或★密度★(taper_density：pebbles/pine_needle/seashells/
#   ground_leaves/ground_twigs/decorative_plants/pinecone/mollusk)。
#   係數在遮罩邊緣正好是 0。
#
#   ⚠️ 這跟 coverage 降門檻★直接打架★：From_Min 就是 select_thresh，把門檻
#      降下去，坡道下緣跟著降 ⇒ ★多爭取到的面積全部落在係數≈0 那一段★。
#      0909 flowers 就是這樣從「0 株」變成「3465 株小不點」，grass 則變成青苔。
#   把係數重映射到 [floor, 1]：官方的淡出還在(中心仍然 1.0、邊緣仍然比較小)，
#   只是不會再歸零。0 = 完全照官方。
TAPER_FLOOR = 0.6


def taper_floor(name: str) -> float:
    """這支 scatter 的邊緣淡出下限。回 0 代表完全照官方。"""
    entry = SCATTERS.get(str(name or "").strip().lower()) or {}
    if not entry.get("return_scalar"):
        return 0.0
    return float(entry.get("taper_floor", TAPER_FLOOR))


# 門檻要跟著★語意等級★縮放(2026-09-10)。
#   上面那張表是在 very_dense 下訂的。director 是照 prompt 措辭選等級的，
#   「a few scattered wildflowers」會選 sparse —— ★那時候撒得少本來就是對的★，
#   用同一個門檻去判會得到一堆「失敗」，而那是 contract 定義錯誤，不是 scatter
#   有問題。
#   ⚠️ ★只往下縮，不往上加★：這個門檻的職責是抓「撒了但看不見」的靜默失敗，
#      是★地板★不是★目標★。往上加會把它變成藝術指標，還會讓已經驗證過的
#      結果(0909 的 18 支、0910 的 12 支都跑 very_dense)重新變成失敗，
#      平白製造要分流的雜訊。「very_dense 撒得夠不夠濃」是另一個問題，
#      要問的話該另外量覆蓋率，不是動這個地板。
MIN_VISIBLE_LEVEL_SCALE = {
    "sparse":     0.3,
    "normal":     1.0,
    "dense":      1.0,
    "very_dense": 1.0,
}


def min_effectively_visible(name: str, level: str | None = None) -> int:
    """(scatter 名, 語意等級) -> 判定「撒得看得出來」所需的畫面內有效實例數。"""
    base = MIN_EFFECTIVELY_VISIBLE.get(
        str(name or "").strip().lower(), DEFAULT_MIN_EFFECTIVELY_VISIBLE)
    key = str(level or DEFAULT_DENSITY_LEVEL).strip().lower()
    scale = MIN_VISIBLE_LEVEL_SCALE.get(key, 1.0)
    return max(1, int(round(base * scale)))


def coverage_escalation_thresholds(name: str, level: str | None) -> list[float]:
    """從 director 選定的等級往下疊的 select_thresh 階梯(含起點)。

    回空 list 代表這一支沒有 select_thresh 可調(catalog 沒記到官方值)，
    ★呼叫端就不要自己編一個★，讓它走官方預設、然後照實回報失敗。
    """
    base = select_thresh_for(name, level)
    if base is None:
        return []
    ladder = []
    for offset in COVERAGE_ESCALATION_OFFSETS:
        value = max(SELECT_THRESH_MIN,
                    min(SELECT_THRESH_MAX, float(base) + offset))
        if value not in ladder:
            ladder.append(value)
    return ladder


def official_mask_params(name: str) -> dict:
    """這支 scatter 在官方 generate_nature.py 裡的遮罩參數(照抄)。

    ★這是我們唯一該有的基準★ —— agent 只選語意等級，其餘一律走這裡的值。
    uniform(a,b) 的取中點，並在該筆的 note 記原始範圍。
    """
    entry = SCATTERS.get(str(name or "").strip().lower()) or {}
    return {
        "mask_scale":    entry.get("mask_scale"),
        "select_thresh": entry.get("select_thresh"),
        "normal_thresh": entry.get("normal_thresh"),
        "return_scalar": entry.get("return_scalar"),
        "terrain_target": entry.get("terrain_target"),
    }


def select_thresh_for(name: str, level: str | None) -> float | None:
    """(scatter 名, 語意等級) -> select_thresh。normal = ★官方原值★。

    回 None 代表 catalog 沒有記到官方值 ⇒ 呼叫端不要傳這個參數，
    讓 placement_mask 走它自己的預設，而不是我們瞎猜一個。
    """
    entry = SCATTERS.get(str(name or "").strip().lower()) or {}
    base = entry.get("select_thresh")
    if base is None:
        return None
    key = str(level or DEFAULT_DENSITY_LEVEL).strip().lower()
    off = SELECT_THRESH_OFFSETS.get(key, 0.0)
    return max(SELECT_THRESH_MIN, min(SELECT_THRESH_MAX, float(base) + off))


def is_official_grime(name: str) -> bool:
    """官方把它當 grime(貼在 populated 資產表面)而不是地形 scatter。

    generate_nature.py 的 grime_types：slime_mold / lichen / ivy / mushroom /
    moss —— 用 scatter_lower / scatter_upward 選面，還有 per-instance 機率。
    我們的 director 另有 placement="grime" 走那條路，這個旗標是提醒：
    ★把它們當 ground_scatter 撒在地形上並不是官方的用法★。
    """
    return bool((SCATTERS.get(str(name or "").strip().lower()) or {}).get("official_grime"))


def density_is_tunable(name: str) -> bool:
    """這個 scatter 的 apply() 收不收 density(且不是死參數)。"""
    entry = SCATTERS.get(str(name or "").strip().lower())
    return bool(entry) and entry.get("density") is not None


def density_semantics(name: str) -> str | None:
    """這支 scatter 的 density ★實際被怎麼使用★ —— 決定倍率的意義。

    2026-09-08 逐支追過 apply() 的函式體，三種互不相容：
      "per_area"   density 直接進 scatter_instances(density=) = 每 m² 個數
                   coral_reef / ground_leaves / mushroom / jellyfish / mollusk
      "per_volume" density 進 vol_density(體積密度，單位不同，且是分布 tuple)
                   ferns / seashells
      "clipped"    density/avg_vol 之後再 clip 到 200 —— ★母本小的時候會飽和★，
                   倍率加大到某個點之後就沒有效果了
                   flowers
    """
    entry = SCATTERS.get(str(name or "").strip().lower())
    return entry.get("density_semantics") if entry else None


def density_for(name: str, level: str | None):
    """(scatter 名, 語意等級) -> 要傳給 apply() 的 density；不可調就回 None。

    ★值域綁在各自的種類上★(2026-09-08 改)。原本是一組共用倍率乘上官方預設，
    但那假設「乘 2.5 倍」對每一支意義相同，而實測不是 —— 見 density_semantics()
    的三種語意。所以 catalog 的每一筆都可以用 density_levels 寫死自己的四個值；
    沒寫的才退回共用倍率(那些是 per_area，倍率的意義最直觀)。

    tuple 形式(("uniform", lo, hi))★整組一起縮放★，保持分布形狀只改尺度。
    """
    entry = SCATTERS.get(str(name or "").strip().lower())
    if not entry or entry.get("density") is None:
        return None
    key = str(level or DEFAULT_DENSITY_LEVEL).strip().lower()
    if key not in DENSITY_LEVELS:
        key = DEFAULT_DENSITY_LEVEL
    per_system = entry.get("density_levels")
    if per_system and key in per_system:
        return per_system[key]
    factor = DENSITY_LEVELS[key]
    base = entry["density"]
    if isinstance(base, tuple):
        return (base[0],) + tuple(float(v) * factor for v in base[1:])
    return float(base) * factor


MODULE_ROOT = "infinigen.assets.scatters"


# Stable identity written on every carrier created by BeyondInfinigen. Native
# Infinigen carrier names describe the generated master factory, not the stage
# that owns them (and can even be shared: scatter:mollusk is seashells in
# generate_nature). Object-name substring matching therefore cannot be the
# create/delete/replace guard.
SEMANTIC_ID_PROPERTY = "bi_scatter_id"

# Compatibility migration for native scenes created before semantic IDs were
# stamped. Patterns are deliberately anchored; loose substring matching made
# mollusk/seashells and plural names silently disagree.
LEGACY_CARRIER_PATTERNS = {
    "grass": (r"^scatter:GrassTuftFactory\(",),
    "flowers": (r"^scatter:FlowerPlantFactory\(",),
    "monocots": (r"^scatter:MonocotFactory\(",),
    "ferns": (r"^scatter:FernFactory\(",),
    "decorative_plants": (r"^scatter:SucculentFactory\(",),
    "ground_leaves": (r"^scatter:LeafFactoryV2\(",),
    "ground_twigs": (r"^scatter:GenericTreeFactory\(",),
    "mushroom": (r"^scatter:mushroom(?:\.\d+)?$",),
    "pinecone": (r"^scatter:pinecone(?:\.\d+)?$",),
    "pine_needle": (r"^scatter:PineNeedleFactory\(",),
    "chopped_trees": (r"^scatter:chopped_tree(?:\.\d+)?$",),
    "pebbles": (r"^scatter:BlenderRockFactory\(",),
    "coral_reef": (r"^scatter:coral(?:\.\d+)?$",),
    "seaweed": (r"^scatter:seaweed(?:\.\d+)?$",),
    "urchin": (r"^scatter:urchin(?:\.\d+)?$",),
    # Native generate_nature has a seashells stage but no mollusk stage. New
    # experimental mollusk carriers are disambiguated by the custom property.
    "seashells": (r"^scatter:mollusk(?:\.\d+)?$",),
    "jellyfish": (r"^scatter:jellyfish(?:\.\d+)?$",),
}


def semantic_id_from_carrier_name(object_name: str) -> str | None:
    """Resolve a legacy native carrier name without fuzzy substrings."""
    value = str(object_name or "")
    hits = [
        semantic_id
        for semantic_id, patterns in LEGACY_CARRIER_PATTERNS.items()
        if any(re.match(pattern, value, re.IGNORECASE) for pattern in patterns)
    ]
    return hits[0] if len(hits) == 1 else None


def canonical_semantic_id(value: str | None) -> str | None:
    """Return a catalog ID or None; callers must not invent aliases."""
    key = str(value or "").strip().lower()
    return key if key in SCATTERS else None


def get(name: str) -> dict | None:
    """scatter 名 -> 目錄項(找不到回 None，不猜)。"""
    entry = SCATTERS.get(str(name or "").strip().lower())
    return dict(entry) if entry else None


def module_path(name: str) -> str | None:
    entry = get(name)
    return f"{MODULE_ROOT}.{entry['module']}" if entry else None


def domain_tag(domain: str | None, *, camera_visible_only: bool = True) -> str:
    """domain -> infinigen tag 字串。無效值退回 land，不猜。

    camera_visible_only=True(預設)會附加 -out_of_view，把畫面外的地形排掉。
    """
    tag = DOMAIN_TAGS.get(str(domain or "").strip().lower(),
                          DOMAIN_TAGS[DEFAULT_DOMAIN])
    if camera_visible_only:
        tag = f"{tag},{CAMERA_VISIBLE_EXCLUDE}"
    return tag


def systems_for_surfaces(surface_types) -> list[str]:
    """場景實際有的 surface 類型 -> 撒得上去的 scatter 系統名(排序後)。

    ★給 director 的選單就是這個★ —— 場景沒有的表面不該出現在選項裡。
    """
    available = {str(s).strip().lower() for s in (surface_types or [])}
    # ★官方當 grime 的那幾支不進 ground_scatter 選單★(2026-09-09)
    #   generate_nature.py 的 grime_types = {slime_mold, lichen, ivy, mushroom,
    #   moss}：它們是 apply 到★populated 的資產表面★(樹、boulder)，用
    #   scatter_lower / scatter_upward 選面，還有 per-instance 機率。
    #   把它們當地形 scatter 撒是我們自己發明的用法，不是官方的。
    #   director 另有 placement="grime" + scatter_region: lower|upward 走那條路。
    #   ⚠️ mushroom ★兩邊都算★ —— 官方既有 run_stage("mushroom") 撒 terrain_near，
    #     也列在 grime_types 裡，所以它保留在選單中。
    eligible = {name for name, entry in SCATTERS.items()
                if not entry.get("official_grime")}
    if not available:
        return sorted(eligible)
    return sorted(name for name in eligible
                  if available & set(SCATTERS[name]["surfaces"]))


def catalog_lines(surface_types=None) -> str:
    """渲染成給 LLM 看的清單(director / composer 共用同一份文字)。"""
    names = systems_for_surfaces(surface_types)
    lines = []
    for name in names:
        entry = SCATTERS[name]
        # ★「不可換外觀」要標得刺眼★：這 6 支沒有 factory_symbol，執行層只會
        #   印一行「不支援換外觀」然後★用官方長相繼續跑★。director 若為了
        #   結構相近而挑中它們(例如「泡泡」挑 pebbles)，最後撒出來的是碎石，
        #   而且不會有任何錯誤。
        swap = "可換外觀" if entry["factory_symbol"] else "★只能官方外觀★"
        dens = "密度可調" if entry["density"] is not None else "密度固定"
        lines.append(f"  {name:20} domain={entry['domain']:10} "
                     f"surfaces={','.join(entry['surfaces']):28} {swap}  {dens}  "
                     f"{entry['note']}")
    return "\n".join(lines)


def tag_keys(tag_dict: dict | None, tag: str) -> list[str]:
    """★照抄 density.tag_mask 的 key 過濾★ —— 用來事前判斷這個 tag 選不選得到東西。

    為什麼需要：placement_mask 對選不到任何 key 的 tag ★不會報錯★，它產生的
    mask 恆為 0，於是 scatter 靜靜地撒出 0 個實例。實測 forest 場景配
    underwater domain 就是 0 個 key。這種靜默失敗要在執行前擋下來。
    """
    if not tag_dict:
        return []
    keys = list(tag_dict)
    for part in str(tag).split(","):
        if part.startswith("-"):
            keys = [k for k in keys if part[1:] not in k.split(".")]
        else:
            keys = [k for k in keys if part in k.split(".")]
    return keys
