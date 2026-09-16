# Property Chat Agent — Python backend

The chat assistant behind the Amira Haddad real estate website (Dubai and Abu Dhabi).
A FastAPI server with a LangGraph agent that answers visitor questions about properties,
using a language model through OpenRouter and the listings stored in Supabase.

The website talks to it through one endpoint, `POST /chat`, and gets back the reply plus
listing cards, a map position, quick-reply chips and any PDF that was made.

## What the assistant can do

- **Find homes** to buy or rent, filtered by area, price, bedrooms, size, type or keywords.
- **Explain a property**: description, amenities, address, photos.
- **Compare properties** and suggest similar ones.
- **Summarise an area**: price range, average price per sqft, which property types are listed.
- **Estimate costs**: for buying, the down payment, government and agency fees and the monthly
  mortgage payment; for renting, the cheques, deposit and fees.
- **Save an enquiry** so the agent can call the visitor back.
- **Write a PDF** (brochure, shortlist, comparison, cost estimate, viewing request or
  requirements summary) and **email it** to the visitor from the agency Gmail account.

Everything the assistant says comes from the database. It cannot invent a listing or a price.

## How a message is answered

```
React website  ──POST /chat──►  FastAPI
                                   │
                                   ▼
                          LangGraph loop (max 6 steps)
                          agent_llm_node ──► tool_node ──┐
                                ▲                        │
                                └────────────────────────┘
                                   │ (the model decides to reply)
                                   ▼
                             finalize_node
                                   │
       reply + cards + map_focus + suggestions + draft ◄──┘
```

The model answers in JSON only: either "call this tool with these arguments" or
"reply to the visitor". Invalid JSON is retried once, then the text is shown as it is.

## Files

| File | What is in it |
| --- | --- |
| `main.py` | Settings, conversation memory, the OpenRouter calls, the LangGraph agent and the FastAPI endpoints |
| `agent/tools.py` | Everything the agent can do: Supabase reads and writes, Gmail sending, price formatting, cost estimates, the PDF builder and the tool list |
| `agent/prompts.py` | The system prompt: who the assistant is and the rules it must follow |
| `requirements.txt` | Pinned Python packages |
| `.python-version` | Python version for Render (3.14) |
| `generated/` | PDFs made while running (safe to delete) |

## Requirements

- Python 3.14
- A Supabase project with the `listings`, `media`, `uae_areas` and `inquiries` tables
- An OpenRouter API key (the default model is free)
- A Google Cloud project with the Gmail API enabled, for the email feature

## Setup

1. **Create a `.env` file** in this folder (settings are listed below). Never commit it.
2. **Install the packages**

   ```bash
   pip install -r requirements.txt
   ```

3. **Start the server**

   ```bash
   python main.py
   ```

   It prints the address to use, for example:

   ```
   Base URL        : http://localhost:8000
   Chat            : POST   http://localhost:8000/chat
   API docs        : http://localhost:8000/docs
   Email           : gmail (the browser opens to sign in on the first email)
   ```

   Copy the `VITE_AGENT_API_URL=...` line it prints into the website `.env` file.
   Open http://localhost:8000/docs to try the endpoints without the website.

## Settings (`.env`)

**Needed**

| Name | What it is |
| --- | --- |
| `OPENROUTER_API_KEY` | Your OpenRouter key, for the language model |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_ANON_KEY` | The **public anon** key. A secret `service_role` key is refused on purpose, so the agent stays inside the database security rules |
| `client_id`, `client_secret` | Google OAuth client, for sending email |

**Optional**

| Name | Default | What it changes |
| --- | --- | --- |
| `BUSINESS_NAME` | Dubai Property Explorer | Name used in replies, PDFs and emails |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Which model answers |
| `LLM_MAX_TOKENS` | 8000 | Longest answer the model may write |
| `LLM_TIMEOUT_SECONDS` | 90 | When to give up on a slow model |
| `LLM_RETRIES` | 2 | Extra tries when the free provider is busy |
| `MAX_TOOL_STEPS` | 6 | Tools per visitor message |
| `SESSION_IDLE_MINUTES` | 120 | How long a quiet conversation is kept |
| `MAX_SESSIONS` | 1000 | Conversations kept in memory at once |
| `MAX_INQUIRIES_PER_SESSION` | 3 | Enquiries one visitor may save |
| `MAX_EMAILS_PER_SESSION` | 3 | Emails one visitor may receive |
| `MAX_EMAILS_PER_DAY` | 20 | Emails the whole server may send per day |
| `GOOGLE_REFRESH_TOKEN` | — | Gmail sign-in for a server, where no browser can open |
| `ALLOWED_ORIGINS` | `*` | Which websites may call the API, comma separated |
| `WEBSITE_URL` | — | Fixes the website address used in PDFs and emails. Without it, the address the browser sends is used |
| `PUBLIC_API_URL` | Render address, or `http://localhost:8000` | Overrides the address used in PDF links |

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/` | Is it running, which model, is email set up |
| `POST` | `/chat` | Answer one visitor message |
| `GET` | `/sessions/{session_id}` | The conversation so far, with its PDFs |
| `DELETE` | `/sessions/{session_id}` | End a conversation |
| `GET` | `/drafts/{draft_id}.pdf` | Download a PDF the assistant made |
| `GET` | `/docs` | Interactive API documentation |

**Request**

```json
{
  "message": "2 bed to rent in Dubai Marina under 200k",
  "session_id": "send back the one you received, or leave it out to start",
  "website_url": "https://your-site.example"
}
```

`website_url` is the address of the website the visitor is on (`window.location.origin` in
React). It is used for the links inside PDFs and emails.

**Reply**

```json
{
  "session_id": "3f1c...",
  "reply": "I found two apartments...",
  "cards": [{ "id": "demo-marina", "title": "...", "price_label": "AED 1.9M", "path": "/property/demo-marina" }],
  "map_focus": { "name": "Dubai Marina", "emirate": "Dubai", "isEmirate": false, "center": [25.08, 55.14] },
  "suggestions": ["Show cheaper options"],
  "draft": null,
  "pending_confirmation": null,
  "inquiry": null,
  "email": null,
  "tools_used": ["search_properties"]
}
```

Cards carry a **path**, not a full address, so the website builds the link on its own domain.

## The tools the model may call

| Tool | What it does |
| --- | --- |
| `search_properties` | Find listings by area, price, bedrooms, size, type or keywords |
| `get_property_details` | Everything about one listing |
| `find_similar_properties` | Listings like a given one |
| `compare_properties` | Two to four listings side by side |
| `list_areas` | The areas the website covers, with how many listings each has |
| `area_market_summary` | Price range, averages and property types for an area |
| `estimate_costs` | Buying or renting costs |
| `save_inquiry` | Save the visitor enquiry (asks them to confirm first) |
| `create_draft` | Make a PDF |
| `update_draft` | Change a PDF |
| `email_draft` | Email the PDF (asks them to confirm first) |

## PDFs and email

A PDF is built with ReportLab from the database, including photos, prices, facts and cost
tables, while the model only writes the wording. It is saved in `generated/` and can be
downloaded from `/drafts/<id>.pdf`.

To let the assistant email PDFs, sign in to Gmail once:

- **On your PC:** the first email opens a Google sign-in in your browser and saves
  `token.json` in the folder the server was started from.
- **On a server:** there is no browser, so set `GOOGLE_REFRESH_TOKEN` (the `refresh_token`
  value inside `token.json`) together with `client_id` and `client_secret`.

Until that is done, the assistant gives the visitor a link to the PDF instead.

> `token.json` and `.env` hold private keys. Keep both out of git.

## Safety

- The agent may **read** only `listings`, `media` and `uae_areas`, and may **add** rows only
  to `inquiries`. Every other table is refused in code, so admin data is out of reach.
- Only the **public anon** Supabase key is accepted, so the database security rules still apply.
- Saving an enquiry and sending an email both need the visitor to **agree in a later
  message**. One message can never both propose and carry out either.
- Per-conversation and per-day limits cap how many enquiries and emails are possible.
- The website address used in PDFs and emails must be a plain `https://host` address.

## Deploying to Render

Render deploys from a Git repository, so push this folder as the repository root.

| Setting | Value |
| --- | --- |
| Type | Web Service |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Instance Type | Free |

Add every setting from your `.env` in the **Environment** tab, plus `GOOGLE_REFRESH_TOKEN`
for email. Render provides the public address itself, so nothing else needs configuring, and
the **Logs** tab shows the `VITE_AGENT_API_URL=...` line for the website.

On the free plan the service sleeps after 15 minutes without traffic and takes about a minute
to wake, and its disk is wiped on every restart, so saved PDFs and running conversations
disappear. Email must use `GOOGLE_REFRESH_TOKEN` there, because the free plan blocks the
usual mail ports.

## If something does not work

| What you see | Why |
| --- | --- |
| `Email : not set up` at start-up | No `token.json` in the folder you started from and no `GOOGLE_REFRESH_TOKEN`. The assistant offers the PDF link instead |
| "The model provider is busy" | The free model is overloaded. It retries twice; try again, or set another `OPENROUTER_MODEL` |
| "The listings database did not respond" | Check `SUPABASE_URL` and `SUPABASE_ANON_KEY`, and that the tables allow public reading |
| `409` from `/chat` | That conversation is still answering the previous message |
| A PDF link says "Draft not found" | The server restarted and its temporary files were cleared |
| Code changes seem to do nothing | Stop the server and start it again, because the automatic reload can miss changes |
