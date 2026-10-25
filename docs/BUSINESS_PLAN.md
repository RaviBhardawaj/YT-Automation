# Business plan: free now, paid later

## What you're selling

An AI video studio for Indian creators who want consistent channels without editing skills: Hindi, Hinglish and
English voices, animated series with recurring characters, upload-and-edit into Shorts, and daily auto-publishing
across several channels. The closest alternatives are general AI video tools (English-first, per-clip pricing)
and freelance editors. Your edge: **Hindi/Hinglish + whole-channel automation**.

## What a video costs you (measured on this studio)

| Item | Real number | Per video |
|---|---|---|
| Voice (Sarvam, ₹30 per 10,000 characters) | median 2,478 characters, max 4,268 | **≈ ₹7, up to ₹13** |
| Scripts and planning (Ollama Cloud) | ~290 calls so far | fixed monthly plan; heavy use needs a paid tier |
| Rendering (this Mac) | edit ≈ 2 min, animated ≈ 10–15 min, researched long ≈ 25 min | electricity; **capacity ≈ 60–100 videos/day** |
| AI video clips (optional, Gemini) | ~$0.10 per second | ≈ ₹65 per 8-second clip; keep for paid plans only |

A Free user making 10 videos a month costs you about **₹70–130**, plus a share of the Ollama plan.

## The biggest limit before scaling: the YouTube upload quota

Every creator uploads through **your** Google Cloud project, and its quota is shared by everyone. The default
quota is 10,000 units/day and an upload costs about 1,600 units, so **only about 6 uploads per day across all
users**. Before inviting many creators:

1. Apply for a quota increase (YouTube API Services audit form). It's free but takes weeks.
2. Get the OAuth app verified (uploading is a sensitive permission: home page, privacy policy, demo video).
3. Until then, keep sign-ups on approval and let most users **download** and upload themselves.

## Pricing (suggestion: test it with the "I'm interested" clicks first)

| Plan | Price / month | Limits (already in the app) | Your cost at full use | Margin |
|---|---|---|---|---|
| Free | ₹0 | 10 videos, 1 channel | ≈ ₹130 | marketing cost |
| Creator | **₹799** | 60 videos, 3 channels | ≈ ₹450–800 | thin; most users won't max out |
| Pro | **₹2,499** | 200 videos, 10 channels, AI clips | ≈ ₹1,500 + AI clips | healthy if AI clips stay capped |
| Studio | **₹7,999** | 1,000 videos, 50 channels | needs a cloud server | for agencies |

Change limits and prices together: the limits live in `studio/plans.py`, and the price lives in the payment
provider.

## Subscriptions vs ads

- **Subscriptions should be the main income.** A few hundred paying creators matter far more than ad revenue.
- **Ads inside the app earn very little.** A logged-in dashboard gets low traffic and AdSense pays per view. The
  ad slot exists (Admin → Website) for Free users, but expect only small amounts. AdSense also has to approve
  your site first, and it requires a privacy policy and real content.
- **Better ad-style income:** a "Made with …" end card on Free videos (free marketing on every upload), affiliate
  links (microphones, hosting), and sponsored templates later.

## Taking payments when you're ready

- **Razorpay** (India: UPI Autopay, cards, subscriptions; easy GST invoices) or **Stripe** (international).
- Flow: the Pricing page has a "Subscribe" button → the provider's checkout → a webhook sets the workspace plan.
  Your manual "Give plan" button on the Admin page keeps working for trials.
- You'll need GST registration once revenue crosses the threshold, plus invoices, a refund policy page and
  correct terms.

## Running it as a business

- **Hosting:** this Mac is fine for a beta of tens of users. Move to a cloud server (and object storage for
  videos) before paid plans. The installers in `install/` make that move straightforward.
- **Approval gate:** keep it on. It protects your API costs and your YouTube quota from spam sign-ups.
- **Policy risks:** YouTube doesn't pay for mass-produced, repetitive "inauthentic" content. Market the product
  as a creative assistant, not "100 channels on autopilot". Kids channels must be marked made-for-kids. Finance
  content should stay educational, never buy/sell calls.
- **Legal:** the privacy policy and terms pages are a starting draft written for this app. Have them reviewed,
  and set your contact email in Admin → Website. India's DPDP Act requires that users can delete their data;
  account deletion is currently done by you on request.

## Growth (cheap first)

1. Your own channels are the demo: put "Made with …" in their descriptions.
2. Short Hindi tutorials ("make a cartoon channel in 10 minutes") on YouTube and Instagram.
3. Creator communities (Telegram, Facebook groups), plus a referral offer: a month of Creator for each friend who
   joins.
4. Watch Admin → plan interest. Launch paid plans only when people are clicking "I'm interested".
