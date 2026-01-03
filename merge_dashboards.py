import json
import os

ROUTER_JSON_PATH = '/home/jesse/develop/router/grafana/provisioning/dashboards/llm-router.json'
CONV_JSON_PATH = '/home/jesse/develop/router/grafana/provisioning/dashboards/conversations.json'

def load_json(path):
    with open(path, 'r') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=4)

def merge_dashboards():
    router_data = load_json(ROUTER_JSON_PATH)
    conv_data = load_json(CONV_JSON_PATH)

    # 1. Extract target panels from conversations.json
    target_ids = [40, 47, 48, 43, 303, 304]
    new_panels = []
    
    # Create a map for quick lookup
    conv_panel_map = {p['id']: p for p in conv_data.get('panels', [])}
    
    for pid in target_ids:
        if pid in conv_panel_map:
            panel = conv_panel_map[pid].copy()
            # Update Datasource
            # datasource could be a dict or string sometimes, but usually dict in recent grafana
            if isinstance(panel.get('datasource'), dict):
                panel['datasource']['uid'] = 'SQLite'
            
            # Reset Y position to 5 (we will determine logic later)
            panel['gridPos']['y'] = 5
            
            new_panels.append(panel)
        else:
            print(f"Warning: Panel {pid} not found in conversations.json")

    # 2. Shift existing panels in llm-router.json
    # We want to insert the new row at y=5.
    # Existing layout:
    # y=0: Row Overview
    # y=1: Current Stats (height 4)
    # y=5: Row Trends (height 1) -> This is where we collide.
    # So everything starting from y=5 needs to be shifted down by 4 (height of new stats).
    
    shift_amount = 4
    
    # Logic to find max id to generate new ids
    current_ids = set()
    
    # Helper to traverse panels (including rows)
    def traverse_update_y(panels, threshold_y, amount):
        for p in panels:
            if 'gridPos' in p:
                if p['gridPos']['y'] >= threshold_y:
                     p['gridPos']['y'] += amount
            
            if 'id' in p:
                current_ids.add(p['id'])
            
            # Handle row panels if they are nested (rare in provisioning but possible)
            if 'panels' in p and isinstance(p['panels'], list):
               traverse_update_y(p['panels'], threshold_y, amount)

    traverse_update_y(router_data.get('panels', []), 5, shift_amount)

    # 3. Insert new panels
    # Assign new IDs
    max_id = max(current_ids) if current_ids else 1000
    
    for i, p in enumerate(new_panels):
        max_id += 1
        p['id'] = max_id
        router_data['panels'].append(p)

    # 4. Sort panels by gridPos y and x to be clean (Optional but good)
    router_data['panels'].sort(key=lambda p: (p.get('gridPos', {}).get('y', 0), p.get('gridPos', {}).get('x', 0)))

    # Save
    save_json(ROUTER_JSON_PATH, router_data)
    print(f"Successfully merged {len(new_panels)} panels into llm-router.json")

if __name__ == "__main__":
    merge_dashboards()
