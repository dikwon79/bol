# Workflow Diagrams

## Full Process Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                    User triggers /orders/sync                     │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
                ┌─────────────────────┐
                │  1. Lagrou Login     │
                │  + SHIPDT Sort Setup │
                └──────────┬──────────┘
                           ▼
                ┌─────────────────────┐
                │  2. Current Month    │◀─── Reverse traversal (newest first)
                │  Extract new entries │     Stop after 5 consecutive processed
                └──────────┬──────────┘
                           ▼
              ┌────────────────────────┐
              │ 3. For each new BL:    │
              │    ┌────────────────┐  │
              │    │ Crawl BL Detail│  │
              │    │ (closebill.asp)│  │
              │    └───────┬────────┘  │
              │            ▼           │
              │    ┌────────────────┐  │
              │    │ Save to DB     │  │
              │    │ (bill_details) │  │
              │    └───────┬────────┘  │
              │            ▼           │
              │    ┌────────────────┐  │
              │    │ Odoo Update    │  │
              │    │ - Find SO      │  │
              │    │ - Check Picking│  │
              │    │ - Match Lots   │  │
              │    │ - Create Lines │  │
              │    │ - Validate     │  │
              │    └───────┬────────┘  │
              │            ▼           │
              │    ┌────────────────┐  │
              │    │ Save Result    │  │
              │    │ success/failed │  │
              │    └───────┬────────┘  │
              │            ▼           │
              │    ┌────────────────┐  │
              │    │ Email Notify   │  │
              │    │ success/failure│  │
              │    └────────────────┘  │
              │        1s delay        │
              │        Next BL →       │
              └────────────────────────┘
                           ▼
                ┌─────────────────────┐
                │  4. Previous Month   │◀─── Same process
                │  Check for missed    │
                └──────────┬──────────┘
                           ▼
                ┌─────────────────────┐
                │  5. Sync Complete    │
                │  Display summary     │
                └─────────────────────┘
```

## Odoo Update Detail Flow

```
Customer PO (7514555143)
        │
        ▼
┌───────────────────┐     Not Found     ┌──────────────┐
│ Find sale.order    │─────────────────▶│ FAILED       │
│ (client_order_ref) │                   │ + failure email│
└───────┬───────────┘                   └──────────────┘
        │ Found (SO24118)
        ▼
┌───────────────────┐     Not Found     ┌──────────────┐
│ Find stock.picking │─────────────────▶│ FAILED       │
│ (origin = SO name) │                   └──────────────┘
└───────┬───────────┘
        │ Found (AD/OUT/04454)
        ▼
┌───────────────────┐     state=done    ┌──────────────┐
│ Check Picking state│─────────────────▶│ SUCCESS      │
│                    │                   │ (Already done)│
└───────┬───────────┘                   └──────────────┘
        │ state=assigned/confirmed
        ▼
┌───────────────────┐
│ Update             │◀── Ship Date (03/06/26)
│ act_delivery_date  │
└───────┬───────────┘
        ▼
┌───────────────────┐
│ Process each item  │
│                    │
│ ① Find stock.lot   │─── Not Found → FAILED
│ ② Match stock.move │─── No Match → FAILED
│ ③ Check stock qty  │─── Insufficient → FAILED
│ ④ Validate demand  │─── Exceeds → FAILED
│ ⑤ Create move.line │
└───────┬───────────┘
        ▼
┌───────────────────┐
│ Validate Picking   │
│ (button_validate)  │
└───────┬───────────┘
        ▼
┌───────────────────┐
│ Post chatter note  │
│ + Send success email│
└───────────────────┘
```

## Page Navigation

```
    ┌─────────────────────────────────────┐
    │           / (Dashboard)              │
    │  ┌──────────┐  ┌──────────────────┐ │
    │  │ Sync Now │  │ Stats Cards      │ │
    │  └────┬─────┘  │ Total|Succ|Fail  │ │
    │       │        └──────────────────┘ │
    │       │        ┌──────────────────┐ │
    │       │        │ Orders Table      │ │
    │       │        │ [BL link] [Status]│ │
    │       │        └───────┬──────────┘ │
    └───────┼────────────────┼────────────┘
            │                │
            ▼                ▼
    ┌──────────────┐  ┌─────────────────┐
    │ /orders/sync │  │ /bills/{bl}     │
    │              │  │                 │
    │ Progress Bar │  │ BL Detail Info  │
    │ SSE Log      │  │ Items Table     │
    │ [→ Dashboard]│  │ [Odoo Send btn] │
    └──────────────┘  │ [← Dashboard]   │
                      └─────────────────┘

    ┌──────────────┐
    │ /db          │  (direct access)
    │ SQL Console  │
    │ Quick Buttons│
    │ [← Dashboard]│
    └──────────────┘
```
