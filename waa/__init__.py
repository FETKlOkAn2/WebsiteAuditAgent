"""
Website Audit Agent — automated cold-outreach pipeline.

Package layout:
  waa.config      project config + .env loading
  waa.core        shared utilities (storage, output)
  waa.discovery   finding prospects (prospector, scraper)
  waa.analysis    auditing + email generation (conversion_audit,
                  personalization, owner_finder, analyzer[/_v2], prompts)
  waa.outreach    delivery (sender, email_validator, replies_monitor)
  waa.proof       annotated screenshots + preview report
  waa.cli         command-line interface (entry: `python -m waa`)
"""
