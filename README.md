# holded-mcp

MCP server for invoicing and treasury management in [Holded](https://www.holded.com):
invoices, purchases, estimates, credit notes, sales receipts, recurring
invoices, contacts, bank accounts and cashflow forecasting.

## Tools (42)

### Sales invoices
| Tool | What it does |
|---|---|
| `list_invoices` | List with filters: start_date, end_date, contact_id, status |
| `get_invoice` | Full detail of an invoice |
| `create_invoice` | Create an invoice (items, taxes, series, due date) |
| `update_invoice` | Add notes/tags or edit fields without wiping the rest |
| `approve_invoice` | Finalize a draft → assigns the definitive invoice number |
| `send_invoice` | Email it to the client |
| `register_invoice_payment` | Register a payment (partial or full) |
| `get_invoice_pdf` | Download the PDF (local file or base64) |
| `attach_document_to_invoice` | Attach a file to an invoice |

### Purchases / expenses
| Tool | What it does |
|---|---|
| `list_purchases` | List with filters: start_date, end_date, contact_id, status |
| `get_purchase` | Full detail of an expense |
| `create_purchase` | Register an expense (with supplier invoice number) |
| `update_purchase` | Add notes/tags/supplier invoice number without wiping the rest |
| `register_purchase_payment` | Register a payment for an expense |
| `attach_document_to_purchase` | Attach a PDF to an expense |

### Estimates
| Tool | What it does |
|---|---|
| `list_estimates` | List with date/status filters |
| `create_estimate` | Create a client estimate/quote |
| `update_estimate` | Add notes/tags or edit fields without wiping the rest |
| `convert_estimate_to_invoice` | Convert an accepted estimate into an invoice |

### Credit notes
| Tool | What it does |
|---|---|
| `list_credit_notes` | List credit notes |
| `create_credit_note` | Create one linked to the original invoice |
| `update_credit_note` | Add notes/tags without wiping the rest |

### Simplified receipts
| Tool | What it does |
|---|---|
| `list_sales_receipts` | List sales receipts |
| `create_sales_receipt` | Issue a simplified receipt |
| `update_sales_receipt` | Add notes/tags without wiping the rest |

### Recurring invoices
| Tool | What it does |
|---|---|
| `list_recurring_invoices` | View active templates |
| `create_recurring_invoice` | Create an automatic invoice (daily/weekly/monthly/yearly) |

### Contacts
| Tool | What it does |
|---|---|
| `list_contacts` | Search by name, email or tax ID |
| `get_contact` | Full contact details |
| `create_contact` | Create a client or supplier |
| `update_contact` | Update contact fields (safe merge: only changes what you pass) |

### Treasury
| Tool | What it does |
|---|---|
| `list_bank_accounts` | Bank accounts with current balance |
| `list_bank_movements` | Movements for an account (filter: `reconciled=False` for pending) |
| `create_bank_movement` | Add a manual movement (income/expense) |
| `reconcile_bank_movement` | Reconcile a movement against an invoice/payment |

### Cashflow
| Tool | What it does |
|---|---|
| `list_cashflow_forecasts` | Forecasted incoming/outgoing payments |
| `create_cashflow_forecast` | Add a manual forecast |

### Remittances
| Tool | What it does |
|---|---|
| `list_remittances` | View active SEPA remittances |

### Configuration
| Tool | What it does |
|---|---|
| `list_taxes` | Tax IDs (VAT, withholding, etc.) |
| `list_services` | Service catalog with prices |
| `list_numbering_series` | Series by document type: `invoice`/`purchase`/`estimate` (`credit-note` and `sales-receipt` return 400) |
| `list_expense_accounts` | Chart-of-accounts expense accounts |

## Typical flows

### Create and send an invoice to a client
```
1. list_contacts(search="client name")           → contact_id
2. list_taxes()                                   → tax ids
3. list_numbering_series("invoice")               → number_line_id
4. create_invoice(contact_id, date, items=[{
     name: "Service", units: 1, price: 800,
     taxes: [vat_id, withholding_id]
   }], number_line_id=...)                        → invoice_id
5. approve_invoice(invoice_id)                    → finalizes with definitive number
6. send_invoice(invoice_id, emails=["client@..."])
```

### Collect a pending invoice
```
1. list_invoices(status="pending", start_date="2026-01-01")
2. register_invoice_payment(invoice_id, amount=..., date="2026-06-17")
```

### Estimate → Invoice
```
1. create_estimate(contact_id, date, items)       → estimate_id
2. send_invoice(estimate_id, emails=[...])         → send to client
3. convert_estimate_to_invoice(estimate_id)        → once accepted
4. approve_invoice(new_invoice_id)
```

## Setup

1. Get your API key: Holded → Settings → Integrations → API.
2. Install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## MCP client configuration

```json
{
  "mcpServers": {
    "holded": {
      "command": "/path/to/.venv/bin/python3",
      "args": ["/path/to/holded-mcp/server.py"],
      "env": {
        "HOLDED_API_KEY": "<your Holded PAT>"
      }
    }
  }
}
```

## Notes and known API quirks

- **Auth:** PAT tokens (`pat_...`) use `Authorization: Bearer <pat_...>`, not
  a `key: value` header.
- **All POST/PUT bodies use snake_case** (`contact_id`, `due_date`,
  `number_line_id`...) — the v2 API rejects camelCase with a 400.
- **No server-side date filtering** — the server paginates through the full
  cursor and filters by date client-side.
- **`draft`** is a boolean flag on the document, not a status value. Valid
  statuses: `pending`, `completed`, `partial`, `cancelled`, `failed`, `overdue`.
- **Purchase reconciliation only works from the UI** — the public API v2
  doesn't support reconciling purchase-type movements
  (`reconcile_bank_movement` with `document_type: "purchase"` always
  produces a zero-amount `forced_reconciled`). Use the Holded UI (Expenses →
  edit payment → select bank account) for that specific case.
- **`register_invoice_payment`/`register_purchase_payment`** use `treasury_id`
  internally (the tool accepts `account_id` and maps it for you); free text
  goes in `description`, not `notes`.
- **No delete endpoint** exists in the API for invoices, purchases, estimates
  or credit notes — only the Holded UI can remove a document.
- **`create_cashflow_forecast`** requires an approved (non-draft) document —
  approve first with `approve_invoice`.
- **Attachments** use `multipart/form-data` with no explicit `Content-Type`
  header (let `requests` set it).

## License

MIT — see [LICENSE](LICENSE).
