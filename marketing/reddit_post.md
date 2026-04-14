# Reddit Post — for r/SaaS or r/Entrepreneur or r/webdev

## Title:
I built an AI agent that finds businesses with bad websites and cold emails them automatically — here's what I learned

## Body:

Hey everyone. I run a small web agency and got tired of manual prospecting, so I built a system to automate it.

**What it does:**
- Searches for local businesses (dentists, med spas, lawyers, etc.)
- Visits their website and checks for real issues (load speed, mobile, SEO signals)
- Scores how likely they are to need help (0-100 based on red flags)
- AI generates a personalized email that references specific problems on their site
- Sends it via SMTP on a daily cron

**What I learned after [X] emails:**

1. **Personalization is everything.** Generic cold email gets 1-2% reply rate. When you mention their actual page speed or missing mobile optimization, reply rates jump to [X]%.

2. **Most small businesses have terrible websites.** I'm not exaggerating. No HTTPS, no mobile layout, copyright 2019 in the footer. It's wild.

3. **The niche matters A LOT.** Med spas and dentists reply. Barbershops and restaurants don't. High-margin businesses care about their online image.

4. **Contact email extraction is the bottleneck.** About 40-60% of small business sites don't have a visible email anywhere. That's the biggest limiter on volume.

5. **AI-generated emails are good enough.** I was skeptical. But when the input is real data about their real site, Claude writes emails that sound genuinely helpful, not spammy.

**Stack:** Python, Claude API, Serper for search, Zoho for sending. Runs daily via GitHub Actions.

**Costs:** ~$2/day in API fees. Each client is worth $500-1000/month. The ROI is absurd.

Happy to answer questions about the approach. Not going to share the repo (it's my competitive advantage lol) but happy to talk about the concepts.

---

# NOTES:
- Reddit hates self-promotion. Frame it as "here's what I learned" not "here's my product"
- Answer EVERY comment in the first 2 hours
- Don't link to anything unless someone specifically asks
- If it gains traction, cross-post to r/Entrepreneur
- Best time: Sunday evening or Monday morning EST
