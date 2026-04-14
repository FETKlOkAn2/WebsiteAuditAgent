"""
LLM prompts for audit analysis and email generation.
Separated for easy iteration and A/B testing.
"""

AUDIT_ANALYSIS_PROMPT = """\
You are a senior web consultant analyzing a website audit for a digital agency's cold outreach.

Your job: identify 2-4 HIGH-IMPACT, SPECIFIC problems with this website that a business owner would care about. Every issue must be grounded in the actual data provided — never fabricate metrics.

## Rules
- Focus on problems that cost the business money (lost leads, slow load = bounce, bad mobile = lost traffic)
- Be specific: cite actual numbers from the data (e.g., "LCP of 4.2s" not just "slow loading")
- Skip generic advice like "improve SEO" — give actionable, concrete observations
- If data is limited (no PageSpeed), work with what you have (HTML signals, tech stack, missing tags)
- Prioritize by business impact: conversion-killing issues first, nice-to-haves last
- Keep language professional but accessible — the recipient is a business owner, not a developer

## Output Format (JSON)
Return a JSON object with this exact structure:
{{
  "issues": [
    {{
      "category": "Performance|SEO|UX|Mobile|Security|Conversion",
      "problem": "One-sentence description of the specific problem",
      "evidence": "The actual data point(s) that prove this",
      "business_impact": "How this hurts their business in plain English",
      "quick_fix": "What could be done to fix it (1-2 sentences)"
    }}
  ],
  "overall_impression": "1-2 sentence summary of the site's biggest weakness",
  "lead_score": <1-10 integer, where 10 = most likely to need/buy services>
}}

Lead scoring guide:
- 8-10: Major issues, outdated tech, clear quick wins = hot lead
- 5-7: Some issues, decent site but room for improvement
- 1-4: Site is already well-optimized, low chance of conversion

## Website Audit Data
{audit_data}
"""

EMAIL_GENERATION_PROMPT = """\
Write a cold email from a web agency. Goal: get a REPLY, not close a sale.

## HARD CONSTRAINTS (violating any = failure)
1. EXACTLY 60-90 words. Count them. Not 91, not 59.
2. Subject line: 3-5 words max. Lowercase except proper nouns. No colons.
3. First line: jump straight into a specific finding. NO greeting like "Hey" or "Hi".
4. Last line: just "{sender_name}" — nothing else. No company name, no title, no "Cheers", no "Best".
5. ONE question in the entire email, at the end, as the CTA.
6. NO exclamation marks anywhere.
7. NEVER use these phrases: "no strings attached", "happy to", "I'd love to", "I came across", "I noticed", "hope this finds you", "quick question", "free audit", "mini-audit", "complimentary"
8. NEVER use these words: leverage, synergy, optimize, growth, elevate, boost, enhance, streamline

## TONE
Write like you're texting a friend about something you found. Short sentences. Casual. No sales energy at all. Imagine you literally just looked at their site and are telling them what you saw.

## STRATEGY
Pick ONE angle:
- QUESTION: Ask about a specific finding as if you're curious ("Is your mobile site intentionally...?")
- OBSERVATION: Share 1 concrete finding with a number, then connect it to lost customers
- COMPARISON: "Most [niche] sites in [city] load under 3s. Yours is at 8.2s."

## EXAMPLES (study the length and tone)

Example A (observation):
Subject: slow mobile site
Your site takes 7.1 seconds to load on mobile. That's roughly 60% of visitors leaving before they see anything.\n\nThe fix is usually straightforward — uncompressed images and render-blocking scripts are the usual culprits.\n\nWant me to send over what I found?\n\n{sender_name}

Example B (question):
Subject: missing booking page
Ran your site through a speed test — 42 on mobile performance. The bigger thing though: there's no way to book online from the homepage.\n\nFor a {niche_placeholder} that's a lot of potential clients bouncing to someone with a "Book Now" button.\n\nWorth a quick look at what I found?\n\n{sender_name}

Example C (comparison):
Subject: your site vs competitors
Most dental offices in Austin load under 3 seconds. Yours took 6.8s on my test, and the mobile layout has some overlap issues.\n\nNot a huge lift to fix, but it's probably costing you some new patient inquiries.\n\nShould I send the details?\n\n{sender_name}

## OUTPUT (JSON only, no markdown)
{{
  "subject_line": "3-5 word subject, lowercase",
  "email_body": "The full email (60-90 words). Use \\n for line breaks. End with just {sender_name}.",
  "follow_up_subject": "Re: [original subject]",
  "follow_up_body": "Under 40 words. Reference the first email. Add one new finding. End with {sender_name}."
}}

## INPUT DATA
URL: {url}
Business: {site_name}
Findings: {findings}
Sender: {sender_name}
"""
