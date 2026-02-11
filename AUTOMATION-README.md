# Election Risk Map — Automated Monitoring Pipeline

## What it does

Every day at 8am ET, a GitHub Action:

1. **Searches** trusted election news sources (Brennan Center, DOJ press releases, Votebeat, Democracy Docket, NPR, AP, and more) via Claude API with web search
2. **Cross-references** every finding against 2+ independent sources
3. **Rates confidence** (HIGH = 3+ sources, MEDIUM = 2 sources)
4. **Opens a GitHub Issue** with verified findings for your review
5. **Skips** anything already on the site (no duplicate alerts)

Nothing goes live without your explicit approval.

## Setup (5 minutes)

### 1. Push your site to GitHub

If you haven't already, create a repo and push your Netlify site files:

```bash
cd your-site-folder
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/electionriskmap.git
git push -u origin main
```

### 2. Add the monitoring files

Copy these into your repo:

```
your-repo/
├── .github/
│   └── workflows/
│       └── monitor.yml          ← GitHub Action workflow
├── scripts/
│   └── monitor.py               ← Monitoring script
├── index.html                    ← Your site
├── methodology.html
├── about.html
├── feed.xml
├── sitemap.xml
├── robots.txt
├── _redirects
└── states/
    └── *.pdf
```

### 3. Add your Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key
2. In your GitHub repo: Settings → Secrets and variables → Actions → New repository secret
3. Name: `ANTHROPIC_API_KEY` — Value: your key

That's it. `GITHUB_TOKEN` is automatically provided by GitHub Actions.

### 4. Connect Netlify to GitHub (optional but recommended)

In Netlify: Site settings → Build & deploy → Link to GitHub repo.
Set publish directory to `/` (root).
Now merging to `main` auto-deploys.

### 5. Test it

Go to your repo → Actions tab → "Election Update Monitor" → Run workflow.
Check the Issues tab after ~1 minute for results.

## How the review flow works

```
GitHub Action runs daily
        ↓
Claude searches + cross-references
        ↓
Issue opened: "🔔 2 election updates found"
        ↓
You read the issue on your phone
        ↓
If accurate → Open Claude conversation:
  "Update electionriskmap.org with findings from Issue #42"
        ↓
Claude updates site + RSS + drafts email
        ↓
You deploy to Netlify
```

## Zero maintenance

The monitor script automatically reads `index.html` to know what's already on the site.
When you update the site and push to GitHub, the monitor immediately knows about the
new entries and won't re-flag them. Nothing to edit manually.

## Cost

- **Claude API:** ~$0.03-0.10 per daily scan (Sonnet with web search)
- **GitHub Actions:** Free for public repos, 2,000 min/month for private repos
- **Total:** ~$2-3/month

## Adjusting frequency

Edit the cron schedule in `.github/workflows/monitor.yml`:

```yaml
# Every 12 hours:
- cron: '0 1,13 * * *'

# Twice a week (Mon, Thu):
- cron: '0 13 * * 1,4'

# Every 6 hours during high-activity periods:
- cron: '0 1,7,13,19 * * *'
```
