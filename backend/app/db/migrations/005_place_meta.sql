-- Migration 005: place_meta 表
-- 存储 POI 营业时间、游玩时长、预约需求等结构化元数据
-- 置信度三级：high（高德API/人工） / medium（RAG抽取） / low（LLM品类默认）

CREATE TABLE IF NOT EXISTS place_meta (
    place_id            TEXT        PRIMARY KEY,
    name                TEXT        NOT NULL,
    city                TEXT        NOT NULL,
    category_l1         TEXT,                       -- 景点 / 餐饮 / 住宿 / 购物
    category_l2         TEXT,                       -- 火锅 / 博物馆 / 咖啡馆 / 主题乐园 / 街区
    open_hours_json     JSONB,                      -- {"mon":[[9,17]],"tue":[[9,17]],...}
    open_hours_conf     TEXT        CHECK (open_hours_conf IN ('high','medium','low')),
    dwell_minutes       INT,
    dwell_conf          TEXT        CHECK (dwell_conf IN ('high','medium','low')),
    need_reservation    BOOLEAN     DEFAULT FALSE,
    reservation_conf    TEXT        CHECK (reservation_conf IN ('high','medium','low')),
    peak_hours_json     JSONB,                      -- [[12,14],[19,21]] 高峰时段（软提示）
    source_breakdown    JSONB,                      -- {"amap":..., "rag":..., "llm_inferred":...}
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_place_meta_city_cat
    ON place_meta(city, category_l1, category_l2);

-- 运行时回填辅助：快速查询某城市缺字段的记录
CREATE INDEX IF NOT EXISTS idx_place_meta_low_conf
    ON place_meta(city) WHERE dwell_conf = 'low' OR open_hours_conf = 'low';

-- ─── 种子数据（low 置信度品类默认值，用于 MVP 回退） ───────────────────────────
-- 这批"虚拟"记录以品类名作为 place_id，Scheduler 在 place_meta 未命中时回退到这里
INSERT INTO place_meta (place_id, name, city, category_l1, category_l2,
    open_hours_json, open_hours_conf, dwell_minutes, dwell_conf)
VALUES
    ('_default_museum',       '博物馆默认',   '_default', '景点', '博物馆',
     '{"mon":null,"tue":[[9,17]],"wed":[[9,17]],"thu":[[9,17]],"fri":[[9,17]],"sat":[[9,17]],"sun":[[9,17]]}',
     'low', 120, 'low'),
    ('_default_theme_park',   '主题乐园默认', '_default', '景点', '主题乐园',
     '{"mon":[[9,21]],"tue":[[9,21]],"wed":[[9,21]],"thu":[[9,21]],"fri":[[9,21]],"sat":[[9,22]],"sun":[[9,22]]}',
     'low', 360, 'low'),
    ('_default_scenic_spot',  '景区默认',     '_default', '景点', '景区',
     '{"mon":[[8,18]],"tue":[[8,18]],"wed":[[8,18]],"thu":[[8,18]],"fri":[[8,18]],"sat":[[8,18]],"sun":[[8,18]]}',
     'low', 120, 'low'),
    ('_default_street',       '街区默认',     '_default', '景点', '街区',
     '{"mon":[[0,24]],"tue":[[0,24]],"wed":[[0,24]],"thu":[[0,24]],"fri":[[0,24]],"sat":[[0,24]],"sun":[[0,24]]}',
     'low', 90, 'low'),
    ('_default_coffee',       '咖啡馆默认',   '_default', '餐饮', '咖啡馆',
     '{"mon":[[9,22]],"tue":[[9,22]],"wed":[[9,22]],"thu":[[9,22]],"fri":[[9,22]],"sat":[[9,22]],"sun":[[9,22]]}',
     'low', 60, 'low'),
    ('_default_restaurant',   '餐厅默认',     '_default', '餐饮', '餐厅',
     '{"mon":[[11,21]],"tue":[[11,21]],"wed":[[11,21]],"thu":[[11,21]],"fri":[[11,21]],"sat":[[11,22]],"sun":[[11,22]]}',
     'low', 75, 'low'),
    ('_default_hotpot',       '火锅默认',     '_default', '餐饮', '火锅',
     '{"mon":[[11,23]],"tue":[[11,23]],"wed":[[11,23]],"thu":[[11,23]],"fri":[[11,23]],"sat":[[11,23]],"sun":[[11,23]]}',
     'low', 90, 'low'),
    ('_default_bar',          '酒吧默认',     '_default', '夜生活', '酒吧',
     '{"mon":[[18,2]],"tue":[[18,2]],"wed":[[18,2]],"thu":[[18,2]],"fri":[[18,3]],"sat":[[18,3]],"sun":[[18,2]]}',
     'low', 120, 'low')
ON CONFLICT (place_id) DO NOTHING;
