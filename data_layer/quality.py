"""
data_layer/quality.py
=====================
sensor_readings.quality 的代碼定義（sql/014）。採集端、寫入排程、網頁共用。

    0 GOOD        正常量測：收到新的樣本，品質良好
    1 HELD        保持值：數值沒有變化、來源確認存活，由心跳 / always 條件以目前時間補寫
    2 UNCERTAIN   不確定：設備回報 Uncertain（例如感測器超出量程、尚在暖機）
    3 BAD         品質不良：設備回報 Bad（感測器故障、斷線），只記錄「轉為不良」的那一筆
    4 COMM_LOST   通訊中斷：採集端連不上設備，記錄中斷發生的時間點（數值沿用最後一筆）
    NULL          v3.1 之前寫入的資料，視為 GOOD

3 / 4 是「標記」而不是量測值：趨勢圖會在這些點斷開線條並標示，報表統計會排除它們。
"""

GOOD = 0
HELD = 1
UNCERTAIN = 2
BAD = 3
COMM_LOST = 4

LABELS = {
    GOOD: "正常",
    HELD: "保持值",
    UNCERTAIN: "不確定",
    BAD: "品質不良",
    COMM_LOST: "通訊中斷",
    None: "正常（舊資料）",
}

# SQL 片段：「是有效量測值」的條件（報表 / 統計用）
VALID_SQL = "(quality IS NULL OR quality < 3)"


def is_valid(quality) -> bool:
    return quality is None or quality < BAD
