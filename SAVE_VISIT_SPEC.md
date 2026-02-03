# Save Minimal Extraction + Cross-Island Visit Spec

## Scope
- Store only **player profile** and **cats static info** (with `cat_memory` as JSON).
- Support **async cross-island visits** (a cat from player A visits player B).
- SQLite remains for Grafana analytics; PostgreSQL stores these records.

## Data Model (PostgreSQL)

### player_profiles
```sql
CREATE TABLE IF NOT EXISTS player_profiles (
  player_id TEXT PRIMARY KEY,
  player_name TEXT,
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### player_cats
```sql
CREATE TABLE IF NOT EXISTS player_cats (
  player_id TEXT NOT NULL,
  cat_id TEXT NOT NULL,
  cat_name TEXT,
  cat_memory JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (player_id, cat_id)
);

CREATE INDEX IF NOT EXISTS idx_player_cats_updated_at ON player_cats (updated_at);
```

### visit_requests
```sql
CREATE TABLE IF NOT EXISTS visit_requests (
  request_id UUID PRIMARY KEY,
  visitor_player_id TEXT NOT NULL,
  target_player_id TEXT NOT NULL,
  cat_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_visit_requests_status ON visit_requests (status);
CREATE INDEX IF NOT EXISTS idx_visit_requests_target ON visit_requests (target_player_id);
```

### island_visitors (materialized snapshot for target island)
```sql
CREATE TABLE IF NOT EXISTS island_visitors (
  target_player_id TEXT NOT NULL,
  visitor_player_id TEXT NOT NULL,
  cat_id TEXT NOT NULL,
  cat_name TEXT,
  cat_memory JSONB NOT NULL,
  arrived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  PRIMARY KEY (target_player_id, visitor_player_id, cat_id)
);

CREATE INDEX IF NOT EXISTS idx_island_visitors_expires ON island_visitors (expires_at);
```

---

## Field Mapping (from `save_file/example.json`)

### Player Profile
| Target Field | Source JSON Path | Notes |
|---|---|---|
| `player_id` | `player.id` | If `player.id` not present, use upload header `X-User-ID` |
| `player_name` | `player.name` or `player.nickname` | Fallback to `player.id` if missing |
| `uploaded_at` | server time | Set on upload |

### Cats
For each entry in `cats` map (keys are cat_id):

| Target Field | Source JSON Path | Notes |
|---|---|---|
| `player_id` | upload header `X-User-ID` | Required |
| `cat_id` | `cats.<catKey>.id` or `<catKey>` | Prefer `id` if present |
| `cat_name` | `cats.<catKey>.name` | Fallback to `cat_id` |
| `cat_memory` | `cats.<catKey>.memory` | Must be JSON; store as JSONB |
| `updated_at` | server time | Set on upload |

If `cat_memory` is missing, store an empty JSON object `{}`.

---

## Async Visit Flow (State Machine)

### States
- `requested`  : request created, waiting for processing
- `validated`  : request validated (both players exist, cat exists)
- `materialized` : visitor snapshot inserted into `island_visitors`
- `active`     : visitor is active on target island
- `expired`    : visitor expired (TTL reached or cleared)
- `failed`     : request failed, `error_message` recorded

### Transitions
1. **Create Request**
   - Input: `visitor_player_id`, `target_player_id`, `cat_id`
   - Create row in `visit_requests` with `status=requested`

2. **Validate**
   - Verify `visitor_player_id` exists in `player_profiles`
   - Verify `target_player_id` exists in `player_profiles`
   - Verify `player_cats` has `(visitor_player_id, cat_id)`
   - If ok -> `status=validated` else `failed`

3. **Materialize Visitor Snapshot**
   - Read `(cat_id, cat_name, cat_memory)` from `player_cats`
   - Upsert into `island_visitors` with `target_player_id` and `expires_at`
   - If ok -> `status=materialized` else `failed`

4. **Activate**
   - Application side can treat `materialized` as `active`
   - Update `visit_requests.status=active`

5. **Expire / Clear**
   - Background job deletes expired visitors or clears explicitly
   - Update `visit_requests.status=expired`

---

## Data Consistency Strategy

### Upload (player + cats)
- Use **upsert** for `player_profiles` by `player_id`.
- Use **upsert** for `player_cats` by `(player_id, cat_id)`.
- If a player uploads partial cats list, only those cats are updated; others remain untouched.
- Optional: if you want strict sync, mark missing cats as inactive (not required now).

### Visit Requests
- Use **idempotency** by generating `request_id` client-side or server-side.
- If the same `(visitor_player_id, target_player_id, cat_id)` is requested while `active`, decide:
  - `ignore` (return existing active)
  - or `refresh` (extend TTL)

### island_visitors
- Upsert on `(target_player_id, visitor_player_id, cat_id)` so re-visits overwrite memory snapshot.
- `expires_at` ensures stale visitors are cleaned.

### TTL Cleanup
- Periodic job: `DELETE FROM island_visitors WHERE expires_at IS NOT NULL AND expires_at < NOW()`
- Optional: update matching `visit_requests.status=expired`

### Conflict Handling
- If `cat_memory` changes after a visit is active:
  - No auto-sync to active visitor (snapshot remains)
  - Re-visit/refresh to update snapshot

---

## Recommended TTL Policy
- Default `expires_at = NOW() + INTERVAL '24 hours'`
- Can be configurable per request

---

## Minimal API (concept)
- `POST /save/upload`
  - Header: `X-User-ID`
  - Body: JSON save file
  - Upsert into `player_profiles`, `player_cats`

- `POST /visit/request`
  - Body: `visitor_player_id`, `target_player_id`, `cat_id`
  - Create `visit_requests`

- `POST /visit/process` (async worker)
  - Finds `requested` rows, runs transitions

- `GET /visit/active?target_player_id=...`
  - Returns visitors from `island_visitors`
