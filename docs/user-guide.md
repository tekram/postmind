# postmind — User Guide

A friendly walkthrough for getting your inbox under control with postmind. Everything
here is done through the web UI — no command line required.

> **The golden rule:** postmind never permanently deletes anything on its own.
> Cleanup moves email to **Trash**, and every action is recoverable from **Undo
> History** for 30 days. Nothing destructive happens without you clicking a
> confirmation.

---

## 1. Getting started

### Connect your account
On first launch you'll be taken through **Onboarding**:

1. **Connect your mailbox** — Gmail (via Google sign-in) or IMAP.
2. **Choose an AI mode** *(optional, can skip)*:
   - **Cloud** — full natural-language Super Agent.
   - **Local** — on-device model; suggests cleanups but you confirm each one.
   - **Off** — no AI; all the manual cleanup tools still work.
3. **Done** — you land on the Dashboard.

> Gmail unlocks the most features (archive, label, real unsubscribe). On IMAP,
> cleanup is trash-only.

### The sidebar
The left sidebar is your main navigation, grouped into sections:

| Link | What it's for |
|------|---------------|
| **✦ Super Agent** | Type what you want in plain English; it analyzes and proposes cleanups. |
| **Dashboard** | At-a-glance inbox overview and recommended next step. |
| **Clean Up** | **The main cleanup screen** — senders ranked by how much space they eat. |
| *Automate* | |
| **Triage** | AI classification of recent emails. |
| **Agents** | Saved automations (e.g. "archive newsletters weekly"). |
| **Watch** | Monitoring rules. |
| *Manage* | |
| **Sync** | Refresh the local cache of your mailbox. |
| **Accounts** | Add / switch mailboxes. |
| **Undo History** | Restore anything you've trashed (30-day window). |
| **Settings** | AI mode, provider, and protected-sender settings. |

> The top three links are the everyday actions; **Clean Up** is highlighted in
> teal because it's where most of your inbox tidying happens.

The badge under the logo shows your current AI mode (**AI cloud / AI local / AI off**).

---

## 2. Clean up old promotional emails (the main task)

This is the most common job and the recommended first thing to do. Use the
**Clean Up** page — it's the most transparent way to bulk-clean.

### Step 1 — Open Clean Up
From the Dashboard, click **"Clean Up →"** (or **Clean Up** in the sidebar).

### Step 2 — Set the filters to surface clutter
At the top of the Clean Up page:
- **Scope** → *Inbox only* or *All mail*
- **Sort** → choose **Impact score** or **Size** to float the biggest offenders up
- **Since** → choose **Last year** or **All time** to target *old* mail
- Click **Scan** if you change the filters.

### Step 3 — Pick the senders
You'll see a table of senders with email count, total size, oldest date, and a
**safety badge**:
- 🟢 **Safe** — promos / newsletters, fine to select
- 🟡 **Review** — glance before selecting
- 🔴 **Sensitive** — banks / legal / health; the checkbox is **disabled** so you
  can't accidentally delete important mail

Tick the checkboxes next to the promotional senders. A bar appears at the bottom
showing **"X senders selected"** with a red **"Move to Trash →"** button.

### Step 4 — Confirm
Click **Move to Trash →**. The **Confirm purge** page shows exactly how many
emails, how much space you'll free, and the full sender list. Review it, then click
the red **"Move X emails to Trash"** button. (Or **Cancel** to back out.)

### Step 5 — Done
You're taken to **Undo History** with a green confirmation banner. That's it — the
promo backlog is in Trash.

---

## 3. Undo anything

Click **Undo History** in the sidebar anytime. Every cleanup is listed with:
- a description (which senders, how many emails),
- when it ran,
- how long until it expires (30 days),
- a **"Restore ↩"** button to put the emails back in your inbox.

Use this if you ever clean up something by mistake.

---

## 4. Using the Super Agent (optional, faster)

If you'd rather describe what you want, open **✦ Super Agent** and type a request.
Example prompts:
- *"What's eating my storage?"*
- *"Find my largest emails"*
- *"Delete everything from no-reply senders"*
- *"Unsubscribe me from newsletters I never open, and trash the back-catalog"*
- *"Create an agent that archives newsletters weekly"*

The agent **proposes** an action as a card and **never executes until you confirm**.
For deletes it routes to the same Confirm purge → Undo flow as the Clean Up page.

> **Note on unsubscribe:** actually unsubscribing is an external action and
> **cannot be undone** (you stop receiving those emails). The optional
> "also trash the back-catalog" part *is* undoable.

---

## 5. Stop the clutter coming back

After clearing the backlog:
- **Unsubscribe** from senders you never read (Super Agent, Gmail only).
- **Create an Agent** to auto-archive recurring promos: ask the Super Agent
  *"Create an agent that archives promotions older than 2 weeks."* Manage saved
  automations on the **Agents** page.

---

## 6. Protecting important senders

To make sure a sender is never offered for cleanup:
- Sensitive senders (bank / legal / health) are auto-flagged 🔴 and locked out.
- You can add your own protected senders so they never appear as selectable —
  see **Settings**.

---

## Quick reference

| I want to… | Do this |
|------------|---------|
| Clear old promos | Dashboard → **Clean Up** → Sort: Impact, Since: Last year → check senders → **Move to Trash →** → Confirm |
| Get something back | **Undo History** → **Restore ↩** |
| Clean up by talking | **Super Agent** → describe it → review card → Confirm |
| Stop future clutter | **Super Agent** → "unsubscribe…" or "create an agent that archives…" |
| Protect a sender | **Settings** → protected senders |
| Change AI mode | **Settings** |
