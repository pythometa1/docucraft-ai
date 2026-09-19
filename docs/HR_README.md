# DocuMind AI for Human Resources

**Turn your own HR letter template and one spreadsheet into a finished, reviewed letter for every person on it. Each letter keeps your letterhead, layout and legal wording exactly as you wrote them.**

Human Resources is the first function available in DocuMind AI. For any document HR doesn't cover, such as certificates, notices, agreements, forms and reports, choose **Other**: it works exactly the same way. The remaining functions on the project screen are marked *Coming soon*.

---

## Contents

1. [What you can produce](#1-what-you-can-produce)
2. [Why teams use it](#2-why-teams-use-it)
3. [Before you start](#3-before-you-start)
4. [Preparing your template](#4-preparing-your-template)
5. [Preparing your spreadsheet](#5-preparing-your-spreadsheet)
6. [Step by step](#6-step-by-step)
7. [Reading the Document Mapping screen](#7-reading-the-document-mapping-screen)
8. [Review, approval and download](#8-review-approval-and-download)
9. [Troubleshooting](#9-troubleshooting)
10. [FAQ](#10-faq)

---

## 1. What you can produce

When you create a project under **Human Resources**, choose one of these document types:

| Document type | Typical use |
|---|---|
| **Offer Letter** | New hires: role, reporting line, start date, pay, and fixed-term or permanent terms |
| **Termination Letter** | Leavers: last working day, notice, final pay and return of property |
| **Promotion Memo** | Internal moves: new title, grade, pay and effective date |
| **Policy Update** | Notices sent to many colleagues about a policy change |
| **HR Letters** | Any other letter your team sends from a standard template |

If your letter isn't listed, pick **HR Letters**. Any Word template works the same way.

---

## 2. Why teams use it

- **Your document stays yours.** Each letter is made from a copy of your own template, so the logo, headers, footers, fonts, tables and page numbers match the master.
- **The right clauses for each person.** Sections that apply only to some people, such as fixed-term or part-time wording, are kept or removed for each person based on their row.
- **Values come only from your data.** Every name, date and salary in a letter comes from a cell in your spreadsheet, and nothing is invented. If a value the letter needs is missing, that letter is held back for you to fix; it is never sent out with a gap.
- **Nothing leaves unapproved.** A letter can't be downloaded until it has been approved, and authors can't close reviews of their own work.
- **Letters in local formats.** Dates, currency and numbers are written in the format for the project's region, for example *2 Nov 2026* and *$158,000.00* for Australia.
- **You can keep working while a template is read.** Reading a new template takes a minute or two. It can run in the background while you do other things.

---

## 3. Before you start

You need:

- **A DocuMind account** in your organisation's workspace. Your administrator creates accounts.
- **The right role.**
  - An **author** uploads templates and data, maps columns and generates letters.
  - A **reviewer / approver** checks letters and approves them.
  - One person can hold both roles, but they still can't close a review of their own letter.
- **Your template**, as a Word file (`.docx` or `.dotx`).
- **Your data**, as a spreadsheet (`.xlsx` or `.csv`) with one row per person. This is often an export from your HR system, such as Workday or SuccessFactors.

---

## 4. Preparing your template

Start from the master template your team already uses. There's no need to rebuild it. The same markings a person would follow by hand also guide DocuMind:

| In your template | Example | What happens |
|---|---|---|
| **Placeholders**, such as coloured text or angle brackets | `<Colleague First Name>`, `<Start Date>` | Replaced with that person's value |
| **Word merge fields** | `«FirstName»` | Replaced with that person's value |
| **Instructions to the person preparing the letter**, often in red | *Include this section only if the colleague is Fixed Term* | Followed, then removed from the finished letter |
| **Everything else** | Legal wording, letterhead, tables, footers | Kept exactly as written |

Tips for the best result:

- **Be consistent.** Mark every placeholder the same way, for example with the same colour or the same brackets.
- **Name the choice in the instruction,** as in *"only if Employment Type is Fixed Term"*. That lets the app link the clause to the matching column in your spreadsheet.
- **Keep one template per letter family.** For example, keep one master for offer letters and a separate one for termination letters.

The full authoring guide is in the app under **Guide**. You need to be signed in to see it.

---

## 5. Preparing your spreadsheet

- Put **one row per letter** and **one column per value**. Column headers go in the first row.
- Column names don't have to match the placeholder names. You'll connect them on the mapping screen, and your choices are remembered for next time.
- For clauses that depend on a choice, use the same value every time, for example always `Fixed Term` or always `Permanent`. Upper and lower case don't matter.
- Extra columns that no letter uses, such as candidate IDs or internal codes, can stay in the file. They're ignored unless you map them.
- **Not sure which columns you need?** After the template is read, download the ready-made spreadsheet from the template. It has one column per value the letter needs, and dropdowns for the choices.

---

## 6. Step by step

The project screen has four stages: **Template → Sources → Document Mapping → Documents**.

### Step 1: Create a project
Go to **Projects → New project**, choose **Human Resources** and a document type, and give the project a name. For example: *Offer letters, ANZ graduate intake, Nov 2026*.

### Step 2: Upload the template
In the **Template** stage, drag in your `.docx`. DocuMind reads it and lists the values it found and the clauses that depend on a choice.

- Reading usually takes one to two minutes.
- Choose **Run in background** to close the window and keep working. A tray in the corner shows progress and tells you when the template is ready.

### Step 3: Add your data
In the **Sources** stage, upload your `.xlsx` or `.csv`. The rows appear with a count of how many letters they will make.

### Step 4: Map the columns
In **Document Mapping → Match columns**, each value in the letter is paired with a column from your spreadsheet. Check any that are flagged (see [section 7](#7-reading-the-document-mapping-screen)), correct any that are wrong, and save. When every value is mapped, the screen shows **"N of N mapped"**.

### Step 5: Generate
Choose **Generate**. DocuMind makes a few letters first as a check, then makes the rest. If one of those first letters has a problem, it stops so that you don't get a whole batch with the same mistake.

### Step 6: Review, approve, download
Open the **Documents** stage to read each letter, send it for review, approve it and download it (see [section 8](#8-review-approval-and-download)).

---

## 7. Reading the Document Mapping screen

Each value in your letter has a status tile beside it:

| Tile | Status | What to do |
|---|---|---|
| ✓ green | **Matched** | Nothing. The column is clearly right. |
| ✓ blue (in a circle) | **Please confirm** | Almost certainly right. Confirm with a click. |
| 👁 amber | **Please check** | A good guess. Check that the column is the one you mean. |
| ? red | **Choose a column** | No match was found. Pick the column yourself. |
| ✎ grey | **Chosen by hand** | You picked this one. It's remembered for next time. |

The line under each value says why its column was suggested:

- **"Column name matches"**: the column header matches the value's name.
- **"Matches the field in your template"**: the column matches a Word merge field in your template.
- **"Matched to this column before in your organisation"**: someone in your organisation confirmed this pairing on an earlier project.

**"Unsure, please check"** means a column has been filled in as a best guess, but it needs your eyes before you generate.

---

## 8. Review, approval and download

Each letter moves through these stages:

**Work in progress → Completed → Approved**

A letter can also be marked **Blocked** (something needs fixing) or **Cancelled**.

- **Request changes.** A reviewer can send a letter back with comments. The letter can't be approved while that request is open.
- **Four-eyes rule on reviews.** If a reviewer sends a letter back, the person who wrote it can't close that review; someone else must.
- **Only approved letters can be downloaded.** This applies to single downloads, bulk downloads and ZIP exports.
- **Formats.** Download each letter as **DOCX** (editable) or **PDF**, or download all approved letters in one ZIP.
- **File names** contain the project and letter numbers, so a downloaded folder stays in order.
- **Audit trail.** Uploads, approvals and downloads are all recorded with who did them and when.

---

## 9. Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| A letter is **Blocked** | A value the letter needs is empty for that person, or a choice column has a value the template doesn't expect, such as `Contract` instead of `Fixed Term`. | Correct the cell in your spreadsheet, upload it again and regenerate that letter. |
| Most values say **Please check** | This is normal for a new template, or for column names that differ from your template's. | Check each one once and save. Next time they'll be suggested with more confidence. |
| A clause appears that shouldn't | The column that decides that clause is mapped to the wrong column. | Open **Document Mapping**, fix the mapping for that choice and regenerate. |
| Dates or currency are in the wrong format | The project's region is wrong. | Ask your administrator to set the project's region. |
| **Download** is greyed out | The letter hasn't been approved yet. | Ask a reviewer to approve it. |
| The template is still being read | Large templates take longer. | Leave it running in the background. The tray tells you when it's done. |

---

## 10. FAQ

**Does DocuMind change my legal wording?**
No. Only the marked placeholders and instructions change. Everything else is copied from your template as it is.

**Does it write any text itself?**
Not in HR letters. Every value comes from your spreadsheet, and every clause comes from your template.

**Can I reuse a template across projects?**
Yes. Templates live in the **Templates** tab and can be used again. Your column choices are remembered across the organisation.

**Can I delete a template?**
Yes. Delete it from the **Templates** tab, one at a time or several at once. A template that approved letters were made from is archived instead of deleted, so those letters stay traceable.

**Who can see our letters?**
Only signed-in members of your organisation's workspace, according to their roles. Letters with personal data can only be downloaded once approved.

**My document isn't an HR letter. Can I still use this?**
Yes. Create the project under **Other** and pick the closest document type, or **General Document**. Everything in this guide applies unchanged.

**When will other functions be available?**
Clinical, Quality, Safety, Medical Affairs, Legal, Regulatory Affairs, Marketing and Finance are in progress. They're shown as *Coming soon* when you create a project.
