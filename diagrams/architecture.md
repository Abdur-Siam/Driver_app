# Diagrams — TOM Driver App

Mermaid sources. Render in any Mermaid-capable viewer (GitHub, VS Code, mermaid.live).

---

## 1. System topology

```mermaid
flowchart TB
  subgraph Device["📱 Driver device — React Native + TypeScript"]
    UI[Screens / UI]
    RQ[react-query cache + local SQLite run]
    LOC[Location service\nbackground GPS + geofences]
    SCAN[Scanner service\nVisionCamera + MLKit]
    POD[POD service\nsignature + photo]
    OUT[(Durable outbox\nordered, idempotent)]
    BUF[(Location ring buffer\nlossy)]
    SEC[Secure store\ntoken + device id]
    UI --> RQ --> OUT
    LOC --> BUF
    SCAN --> OUT
    POD --> OUT
  end

  subgraph TOM["🟦 TOM backend — Flask / Gunicorn (multi-worker)"]
    API["driver_api_v1\n/api/driver/v1/*"]
    AUTH[driver_auth_store]
    RUN[spine board\n_merged_job_list / multidrop / sequence]
    MUT["jobs_core.mutations\n+ driver_actions audit"]
    LOCING[location ingest]
    OPT[route optimiser proxy]
    PUSH[push dispatcher]
    API --> AUTH
    API --> RUN
    API --> MUT
    API --> LOCING
    API --> OPT
  end

  subgraph Data["🗄️ Postgres via PgBouncer (txn pool) — dual SQLite/PG"]
    JOBS[(jobs / drivers / driver_actions)]
    DL[(driver_locations + latest)]
    PE[(parcel_events)]
    IDK[(idempotency_keys)]
    MEDIA[(object storage: POD media)]
  end

  GMP["🗺️ Google Maps Platform\nRoutes API · Route Optimization API\nDistance Matrix · Geocoding · Maps SDK"]

  Device -- HTTPS JSON --> API
  PUSH -- FCM/APNs --> Device
  LOCING --> DL
  MUT --> JOBS
  API --> PE
  API --> IDK
  POD -. multipart .-> MEDIA
  OPT -- server-side, keyed --> GMP
  RUN --> DL
```

## 2. Job lifecycle (status machine)

```mermaid
stateDiagram-v2
  [*] --> ACCEPTED
  ACCEPTED --> ON_ROUTE_TO_PU: Start / Navigate
  ON_ROUTE_TO_PU --> ARRIVED: geofence + confirm
  ARRIVED --> POB: all parcels scanned (collect)
  POB --> ON_ROUTE_POB: depart pickup
  ON_ROUTE_POB --> POD: scan + signature + photo (per drop)
  POD --> ON_ROUTE_POB: more drops remain
  POD --> COMPLETED: last drop done
  ARRIVED --> COA: cancelled on arrival
  ON_ROUTE_TO_PU --> CANCELLED: ops cancel
  COMPLETED --> [*]
  note right of POD
    multi-drop: scan guards the
    right parcel to the right drop
  end note
```

## 3. Offline write path (durable, idempotent)

```mermaid
sequenceDiagram
  participant U as Driver action
  participant O as Outbox (SQLite)
  participant D as Drainer
  participant S as TOM /api/driver/v1
  participant DB as Postgres + audit

  U->>O: write event row (uuid = idempotency key) + stage media
  U-->>U: optimistic UI update
  loop when online
    D->>O: next pending (in created_at order)
    D->>S: POST with X-Idempotency-Key
    alt first apply
      S->>DB: apply + driver_actions + store result by key
      S-->>D: 2xx result
    else retry (already applied)
      S-->>D: 2xx stored result (no double-apply)
    end
    D->>O: mark done, delete media
  end
```

## 4. Route optimisation flow

```mermaid
sequenceDiagram
  participant A as App
  participant T as TOM route optimiser
  participant G as Google Maps Platform
  participant DB as jobs.sequence_position

  A->>T: POST /route/optimise (current position, constraints)
  T->>G: Geocode missing drop coords (if any)
  T->>G: Distance Matrix (time/distance)
  T->>G: Routes API optimizeWaypointOrder (or Route Optimization API for fleet)
  T->>T: apply TOM constraints (deadlines, locked-first, multidrop preserve)
  T->>DB: persist ordered sequence
  T-->>A: ordered dockets + legs + ETAs (route.version++)
  Note over A,DB: board reads the same sequence — app & ops agree
```
