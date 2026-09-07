# OFAC screening

Check people or organisations against OFAC sanctions lists from your agent, with a dated report and the underlying matching evidence saved locally. Screen one payee or a batch of up to 500.

## Example

```text
Use ofac-screen to look up the fictional organisation
"Zzxqv Prototype Fixture 7f3e9a" and save the screening report.
```

In a live API check on 7 September 2026, this fictional control returned:

| Name supplied | Result | Matches |
| --- | --- | --- |
| Zzxqv Prototype Fixture 7f3e9a | No potential matches returned | 0 |

Scope: OFAC SDN and consolidated non-SDN lists. Minimum similarity score: 95. Results can change as the lists change.

The skill saves the submitted identifiers and settings, vendor response, summary and Markdown report. Potential matches are presented for human review; failed or incomplete requests remain unresolved.

## Requirements

- An agent that can load skills, create local files and run Python commands.
- Python 3.10+ on macOS or Linux. Tested on macOS; no Python packages are required. File permissions assume a POSIX filesystem.
- Your own [OFAC-API.com account and API key](https://www.ofac-api.com/pricing). The service offers a limited trial; ongoing use requires a paid plan. Each submitted case counts towards your allowance.
- Node.js/npm for the installation command below.

Names and any supplied identifiers are sent to OFAC-API.com, a commercial provider, and retained in local reports. Configure the key through your agent's environment or secret manager as `OFAC_API_KEY`; keep it out of chat and source files. 1Password is optional.

## Installation

```bash
npx skills add HartreeWorks/skill--ofac-screen
```

Restart your agent session if the new skill does not appear.

## Try it

Ask your agent:

```text
Use ofac-screen to check the fictional organisation
"Zzxqv Prototype Fixture 7f3e9a". Use my configured API key,
show the screening result, and link to the saved report.
```

For a real lookup, replace the fictional name with the payee's legal name. Add their date of birth or other known identifiers when helpful. The skill supports name-only searches and asks when supplied details are ambiguous.

Expect one of three outcomes: **no potential matches returned**, **potential matches—review needed**, or **screening failed**. Reports are saved under `~/.local/share/ofac-screen/runs` by default; the output directory is configurable.

## Scope and limits

This is a prototype screening aid. It does not approve payments or make final compliance decisions. The default score of 95 follows the vendor recommendation; organisations should validate the matching settings and acceptable data freshness before operational use. Scores describe similarity, not the probability that two identities are the same.

A negative name search does not resolve ownership-based restrictions or all country/activity restrictions. Saved evidence is editable local data, not a tamper-proof audit system. Potential matches need a responsible human's review.

## Documentation

See [SKILL.md](./SKILL.md) for complete instructions, input fields and interpretation guidance.

Run the synthetic tests without an API key:

```bash
python3 -m unittest discover -s tests -v
```

## About

Created by [Peter Hartree](https://x.com/peterhartree) of [AI Wow](https://wow.pjh.is).

Find more skills at [HartreeWorks/skills](https://github.com/HartreeWorks/skills).
