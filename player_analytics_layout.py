"""Legacy-style presentation for the current player analytics query contracts."""
import copy


def apply_player_layout(boards):
    """Keep every query and field; arrange summary cards and compact, grouped tables."""
    for uid in ('gameplay-overview', 'gameplay-player-detail', 'gameplay-players',
                'gameplay-player-days', 'gameplay-player-events'):
        board = boards[uid + '.json']
        panels = {p['id']: p for p in board['panels']}
        arranged = []
        y = 0
        row_id = 2000

        def section(title):
            nonlocal y, row_id
            arranged.append({'id': row_id, 'type': 'row', 'title': title,
                             'collapsed': False, 'panels': [],
                             'gridPos': {'x': 0, 'y': y, 'w': 24, 'h': 1}})
            row_id += 1
            y += 1

        def line(ids, height=8, widths=None):
            nonlocal y
            widths = widths or [24 // len(ids)] * len(ids)
            x = 0
            for pid, width in zip(ids, widths):
                p = panels.pop(pid)
                p['gridPos'] = {'x': x, 'y': y, 'w': width, 'h': height}
                if p['type'] == 'table' and width <= 12:
                    p['fieldConfig']['defaults']['custom'].setdefault('minWidth',70)
                arranged.append(p)
                x += width
            y += height

        def stat(pid, color='green'):
            p = panels[pid]
            p['type'] = 'stat'
            p['options'] = {
                'colorMode': 'value', 'graphMode': 'none', 'justifyMode': 'center',
                'orientation': 'auto', 'textMode': 'value_and_name',
                'wideLayout': True, 'showPercentChange': False,
                'text': {'titleSize': 13, 'valueSize': 32},
                'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
            }
            defaults = p['fieldConfig']['defaults']
            defaults.pop('custom', None)
            defaults['color'] = {'mode': 'fixed', 'fixedColor': color}
            # Sample coverage remains visible, but is visually secondary to the metric.
            p['fieldConfig']['overrides'].append({
                'matcher': {'id': 'byRegexp', 'options': '.*(有效玩家数|有效快照数|样本数)$'},
                'properties': [{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'gray'}}],
            })

        if uid == 'gameplay-overview':
            # Twelve separate cards mirror the original 4 + 6 + 2 grid exactly.
            specs = [(1,100,'总玩家数','玩家数'),(2,100,'昨日新增','昨日新增'),
                     (3,100,'七日新增','7日新增'),(4,100,'三十日新增','30日新增'),
                     (5,102,'中位数','时长中位数（分钟）'),(6,102,'平均数','时长平均数（分钟）'),
                     (7,103,'中位数','游玩天数中位数'),(8,103,'平均数','游玩天数平均数'),
                     (9,104,'中位数','岛屿等级中位数'),(10,104,'平均数','岛屿等级平均数'),
                     (11,101,'总金币余额','总金币数'),(12,101,'拥有猫总数','猫总数')]
            for pid,source,field,title in specs:
                panels[pid] = copy.deepcopy(panels[source])
                panels[pid].update(id=pid,title=title)
                panels[pid]['transformations'] = [{'id':'filterFieldsByName','options':{'include':{'names':[field]}}}]
                stat(pid)
                panels[pid]['options']['textMode'] = 'value'
                panels[pid]['options'].pop('text',None)
            section('总览')
            line([1,2,3,4],4)
            line([5,6,7,8,9,10],4)
            line([11,12],4)
            line([120,14,126])
            line([127,141,128])
            section('AI Token / 成本')
            for pid in (20,21,22,23):
                stat(pid)
                panels[pid]['options']['textMode'] = 'value'
            line([20,21,22,23],4)
            line([24,25])
            line([26,27])
            section('玩家一览')
            line([28,29])
            line([19],12)

            section('职业与玩家行为')
            line([105,140])
            section('商店与招募 · Top 5')
            line([122,124,121])
            line([123,125],widths=[8,8])
            section('钓鱼 · 区域与鱼影')
            line([142,154],widths=[14,10])
            section('猫的工作与猫舍')
            line([143,150,144])
            line([151],6)
            section('穿搭、交流与小剧场')
            line([152,145],10,[14,10])
            line([226],9)
            section('指标覆盖与历史说明')
            line([100,101],6)
            line([102,103,104],6)
            line([153,30],6)
            line([31],9)
            line([999],4)
        elif uid == 'gameplay-player-detail':
            section('玩家基础数据')
            line([100], 5)
            line([101, 102], 9, [8, 16])
            line([103, 104], 8)
            section('经营与行为 · Top 5')
            line([120, 140, 126])
            line([127, 141, 128])
            section('商店与招募 · Top 5')
            line([122, 124, 121])
            line([123, 125], widths=[8, 8])
            section('钓鱼、猫技能与输入上下文')
            line([142], 8)
            line([143, 144, 145])
            section('小剧场')
            line([222, 224], 9, [16, 8])
            line([223], 9)
            line([225, 221], 8)
            line([226], 12)
            section('完整行为明细')
            line([160], 12)
            section('统计口径与历史说明')
            line([999], 4)
        else:
            section({'gameplay-players': '玩家一览', 'gameplay-player-days': '游戏日一览',
                     'gameplay-player-events': '全部事件'}[uid])
            line([100], 18)
            if uid == 'gameplay-player-days':
                section('历史日记录 · 保留原始会话')
                line([101], 18)
            section('统计口径与历史说明')
            line([999], 4)

        # Future query panels must be explicitly placed instead of silently disappearing.
        if panels:
            raise ValueError(f'Unplaced panels in {uid}: {list(panels)}')
        board['panels'] = arranged
