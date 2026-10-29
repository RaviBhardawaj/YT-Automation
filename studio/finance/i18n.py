"""Hindi / English text for NIFTY pivot episodes.

Deterministic wording only (observations, limitations, title, intro, description, charts).
Numbers keep Western digits with Indian-English separators, as Hindi financial media does;
the voice reads them without separators (see media.tts.spoken_numbers).
"""
from __future__ import annotations

from datetime import date

HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def code(language: str) -> str:
    return "hi" if language in ("Hindi", "Hinglish") else "en"


def date_label(iso: str, lang: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {HI_MONTHS[d.month - 1]} {d.year}" if lang == "hi" else f"{d.day} {d.strftime('%b %Y')}"


def short_date(iso: str, lang: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {HI_MONTHS[d.month - 1]}" if lang == "hi" else f"{d.day} {d.strftime('%b')}"


def nature(data_nature: str, lang: str) -> str:
    en = {"end_of_day": "end-of-day", "delayed": "delayed", "intraday": "intraday"}
    hi = {"end_of_day": "एंड-ऑफ़-डे", "delayed": "विलंबित", "intraday": "इंट्राडे"}
    return (hi if lang == "hi" else en).get(data_nature, data_nature)


T: dict[str, dict[str, str]] = {
    "change": {
        "en": "On {date} NIFTY 50 closed at {close}, {dir} {pts} points ({pct}%) from the previous close of {prev}.",
        "hi": "{date} को NIFTY 50 {close} पर बंद हुआ, जो पिछले क्लोज़ {prev} से {pts} अंक ({pct}%) {dir} था।"},
    "up": {"en": "up", "hi": "ऊपर"}, "down": {"en": "down", "hi": "नीचे"},
    "close_location": {
        "en": "The close was in the {third} of the day's {range}-point range (about {pct}% of the way from low to high).",
        "hi": "क्लोज़ दिन की {range} अंकों की रेंज के {third} में रहा (लो से हाई की ओर लगभग {pct}%)।"},
    "third_upper": {"en": "upper third", "hi": "ऊपरी तिहाई हिस्से"},
    "third_middle": {"en": "middle third", "hi": "बीच वाले तिहाई हिस्से"},
    "third_lower": {"en": "lower third", "hi": "निचले तिहाई हिस्से"},
    "range_vs_avg": {
        "en": "That range was {size} the 20-session average range of {avg} points (ratio {ratio}). Wider ranges "
              "produce wider gaps between pivot levels.",
        "hi": "यह रेंज पिछले 20 सत्रों की औसत रेंज {avg} अंकों से {size} थी (अनुपात {ratio})। रेंज जितनी चौड़ी होती है, "
              "पिवट स्तरों के बीच दूरी उतनी ज़्यादा होती है।"},
    "wider": {"en": "wider than", "hi": "ज़्यादा चौड़ी"}, "narrower": {"en": "narrower than", "hi": "संकरी"},
    "similar": {"en": "close to", "hi": "लगभग बराबर"},
    "level_spacing": {
        "en": "With a {range}-point range, S2 at {s2} and R2 at {r2} sit {w} points apart around the pivot of {p}.",
        "hi": "{range} अंकों की रेंज के साथ S2 {s2} पर और R2 {r2} पर है — पिवट {p} के आसपास दोनों के बीच {w} अंकों का अंतर है।"},
    "gap": {
        "en": "{rdate} opened at {open}, {kind} of {pts} points from the basis close.",
        "hi": "{rdate} को बाज़ार {open} पर खुला — पिछले क्लोज़ से {pts} अंकों का {kind}।"},
    "gap_up": {"en": "a gap up", "hi": "गैप-अप"}, "gap_down": {"en": "a gap down", "hi": "गैप-डाउन"},
    "gap_none": {"en": "unchanged", "hi": "बदलाव नहीं"},
    "gap_flat": {"en": "{rdate} opened at {open}, exactly at the basis close — no opening gap.",
                 "hi": "{rdate} को बाज़ार {open} पर, ठीक पिछले क्लोज़ पर खुला — कोई ओपनिंग गैप नहीं।"},
    "reached": {
        "en": "{name} at {val} was reached during {rdate}, and the session closed {side} it at {close}{held}.",
        "hi": "{rdate} को {name} ({val}) तक बाज़ार पहुँचा, और सत्र इसके {side} {close} पर बंद हुआ{held}।"},
    "above": {"en": "above", "hi": "ऊपर"}, "below": {"en": "below", "hi": "नीचे"},
    "held": {"en": " — the level held on a closing basis", "hi": " — क्लोज़िंग आधार पर यह स्तर टिका रहा"},
    "not_tested": {
        "en": "{name} at {val} was not tested on {rdate}; the session's {edge} stayed {dist} points away.",
        "hi": "{rdate} को {name} ({val}) तक बाज़ार नहीं पहुँचा; सत्र का {edge} इससे {dist} अंक दूर रहा।"},
    "low": {"en": "low", "hi": "लो"}, "high": {"en": "high", "hi": "हाई"},
    "touched": {
        "en": "Levels inside the {rdate} range ({low} to {high}): {levels}.",
        "hi": "{rdate} की रेंज ({low} से {high}) के भीतर आए स्तर: {levels}।"},
    "touched_none": {
        "en": "No pivot level fell inside the {rdate} range of {low} to {high}.",
        "hi": "{rdate} की रेंज ({low} से {high}) में कोई पिवट स्तर नहीं आया।"},
    "intraday": {"en": "On the verified {tf} chart, levels were first reached at: {list}.",
                 "hi": "सत्यापित {tf} चार्ट पर स्तर पहली बार यहाँ पहुँचे: {list}।"},
    "lv_tested_held": {
        "en": "On the 15-minute chart of {rdate}, {name} at {val} was first touched at {t} IST ({n} bars touched it), and no 15-minute bar closed {dir} it.",
        "hi": "{rdate} के 15-मिनट चार्ट पर {name} ({val}) को पहली बार {t} IST पर छुआ गया ({n} बार ने इसे छुआ), और कोई भी 15-मिनट बार इसके {dir} बंद नहीं हुआ।"},
    "lv_closed_beyond": {
        "en": "On the 15-minute chart of {rdate}, {name} at {val} was first touched at {t} IST; the first 15-minute close {dir} it came at {tb} IST, and the session finished {dir} it.",
        "hi": "{rdate} के 15-मिनट चार्ट पर {name} ({val}) को पहली बार {t} IST पर छुआ गया; इसके {dir} पहला 15-मिनट क्लोज़ {tb} IST पर आया, और सत्र भी इसके {dir} ख़त्म हुआ।"},
    "lv_failed": {
        "en": "{name} at {val} on {rdate}: a 15-minute bar closed {dir} it at {tb} IST, but a later bar closed back at {tr} IST and the session finished on its original side. In the 15-minute data this was a failed breakout.",
        "hi": "{rdate} को {name} ({val}): एक 15-मिनट बार {tb} IST पर इसके {dir} बंद हुआ, लेकिन {tr} IST पर एक बाद का बार वापस लौट आया और सत्र अपनी मूल दिशा में ख़त्म हुआ। 15-मिनट डेटा में यह एक विफल ब्रेकआउट था।"},
    "lv_not_tested_15": {
        "en": "{name} at {val} was not reached on the 15-minute chart of {rdate}; the closest approach stayed {dist} points away.",
        "hi": "{rdate} के 15-मिनट चार्ट पर {name} ({val}) तक बाज़ार नहीं पहुँचा; सबसे नज़दीकी दूरी {dist} अंक रही।"},
    "struct_15": {
        "en": "The 15-minute bars of {rdate} formed a {label} sequence: the session high of {high} came at {ht} IST and the low of {low} at {lt} IST.",
        "hi": "{rdate} के 15-मिनट बार ने {label} क्रम बनाया: सत्र का हाई {high} {ht} IST पर और लो {low} {lt} IST पर आया।"},
    "struct_1h": {
        "en": "On the 1-hour chart of {rdate}, the {n} bars showed a {label} sequence: of {steps} bar-to-bar steps, {hh} made a higher high and {ll} made a lower low.",
        "hi": "{rdate} के 1-घंटे चार्ट पर {n} बार ने {label} क्रम दिखाया — {steps} बार-दर-बार बदलावों में {hh} बार ऊँचा हाई और {ll} बार निचला लो बना।"},
    "s_rising": {"en": "rising (higher highs and higher lows)", "hi": "ऊपर की ओर (ऊँचे हाई और ऊँचे लो)"},
    "s_falling": {"en": "falling (lower highs and lower lows)", "hi": "नीचे की ओर (निचले हाई और निचले लो)"},
    "s_mixed": {"en": "mixed, two-sided", "hi": "मिला-जुला, दोनों तरफ़ा"},
    "s_insufficient": {"en": "too short to describe", "hi": "बहुत छोटा"},
    "cmp_disagree": {
        "en": "The two timeframes disagreed at {levels}: what the 15-minute chart showed as a close beyond the level did not appear the same way on the 1-hour chart.",
        "hi": "{levels} पर दोनों टाइमफ़्रेम सहमत नहीं थे: 15-मिनट चार्ट पर जो क्लोज़ स्तर के पार दिखा, वह 1-घंटे चार्ट पर उसी तरह नहीं दिखा।"},
    "cmp_agree": {
        "en": "At {levels}, the 15-minute and 1-hour charts told the same story about closes beyond the level.",
        "hi": "{levels} पर 15-मिनट और 1-घंटे चार्ट ने स्तर के पार क्लोज़ के बारे में एक जैसी तस्वीर दिखाई।"},
    "mtf": {
        "en": "Using the last {n} completed {tf} bars up to the {basis} close, the {tf} Fibonacci levels were S2 {s2}, pivot {p} and R2 {r2}.",
        "hi": "{basis} के क्लोज़ तक के आख़िरी {n} पूरे {tf} बार से {tf} फिबोनाची स्तर थे: S2 {s2}, पिवट {p} और R2 {r2}।"},
    "mtf_same": {"en": " These match the daily levels, because 25 fifteen-minute bars cover exactly one full session.",
                 "hi": " ये दैनिक स्तरों के बराबर हैं, क्योंकि 25 पंद्रह-मिनट बार ठीक एक पूरे सत्र को कवर करते हैं।"},
    "next_levels": {
        "en": "From the high, low and close of {rdate} (range {rng} points), the Fibonacci reference levels for {ndate} are S2 {s2}, S1 {s1}, pivot {p}, R1 {r1} and R2 {r2}.",
        "hi": "{rdate} के हाई, लो और क्लोज़ (रेंज {rng} अंक) से {ndate} के फिबोनाची संदर्भ स्तर हैं: S2 {s2}, S1 {s1}, पिवट {p}, R1 {r1} और R2 {r2}।"},
    "next_position": {
        "en": "The {rdate} close of {close} sits {side} the {ndate} pivot of {p}, by {d} points.",
        "hi": "{rdate} का क्लोज़ {close}, {ndate} के पिवट {p} से {d} अंक {side} है।"},
    "lim_next": {"en": "{ndate} has not happened yet: its levels are reference zones to watch, not predictions of where the index will go.",
                 "hi": "{ndate} का सत्र अभी हुआ नहीं है: इसके स्तर देखने लायक संदर्भ ज़ोन हैं, यह भविष्यवाणी नहीं कि इंडेक्स कहाँ जाएगा।"},
    # Search-friendly but factual: what viewers type ("nifty levels", "support resistance") and the real dates
    "title_review": {"en": "NIFTY 50 Levels for {ndate} | {date} Full Analysis | Fibonacci Pivot Support & Resistance",
                     "hi": "NIFTY 50 {ndate} के लेवल्स | {date} का पूरा एनालिसिस | फिबोनाची पिवट सपोर्ट रेजिस्टेंस"},
    # YouTube shows the first three above the title; more than 15 hashtags are all ignored, so keep it to ~9
    # News-anchor style sign-offs (spoken after the disclosure, over a closing card)
    "signoff_review": {"en": "That was the full analysis for {date}, with the levels for {ndate}. Join us again on the next "
                             "trading day, same time, for the next update. Until then — goodbye, and take care.",
                       "hi": "तो ये था {date} का पूरा एनालिसिस, और {ndate} के लेवल्स। ऐसे ही डेली अपडेट्स के लिए जुड़े "
                             "रहिए — मिलते हैं अगले ट्रेडिंग डे, इसी समय। तब तक के लिए, नमस्कार!"},
    "signoff_episode": {"en": "That's all for this episode. See you in the next one — until then, goodbye and take care.",
                        "hi": "तो ये था आज का एपिसोड। मिलते हैं अगले वीडियो में — तब तक के लिए, नमस्कार!"},
    "signoff_lesson": {"en": "That was part {n} of {total}. The next part comes on the next day at 4 PM — until then, goodbye.",
                       "hi": "तो ये था पार्ट {n} ऑफ़ {total}। अगला पार्ट अगले दिन शाम 4 बजे — तब तक के लिए, नमस्कार!"},
    "signoff_series_end": {"en": "And that completes the series. Thank you for learning with us — goodbye, and take care.",
                           "hi": "और इसी के साथ ये सीरीज़ पूरी हुई। साथ सीखने के लिए शुक्रिया — नमस्कार!"},
    "signoff_short": {"en": "See you in the next update — goodbye!", "hi": "मिलते हैं अगले अपडेट में — नमस्कार!"},
    "c_signoff_next": {"en": "Next update: {ndate}", "hi": "अगला अपडेट: {ndate}"},
    "c_signoff_sub": {"en": "Subscribe for daily NIFTY 50 analysis", "hi": "डेली NIFTY 50 एनालिसिस के लिए सब्सक्राइब करें"},
    "hashtags": {"en": "#NIFTY50 #NiftyAnalysis #StockMarketIndia #NiftyLevels #SupportAndResistance #FibonacciPivot "
                       "#PivotPoints #TechnicalAnalysis #ShareMarket",
                 "hi": "#NIFTY50 #NiftyAnalysis #ShareMarket #NiftyLevels #SupportResistance #FibonacciPivot "
                       "#PivotPoints #StockMarketHindi #TechnicalAnalysis"},
    "hashtags_short": {"en": "#NIFTY50 #Shorts #NiftyLevels #StockMarketIndia #FibonacciPivot #ShareMarket",
                       "hi": "#NIFTY50 #Shorts #ShareMarket #NiftyLevels #FibonacciPivot #StockMarketHindi"},
    "intro_review": {
        "en": "Here is the NIFTY 50 session of {review}, reviewed against the Fibonacci pivot levels calculated from {basis}, using {nature} data. After that, we calculate the reference levels for {ndate}.",
        "hi": "नमस्ते दोस्तों! आज, {review} के NIFTY 50 सेशन को हम उन फिबोनाची पिवट लेवल्स पर चेक करेंगे जो {basis} के डेटा से निकले थे — {nature} डेटा के साथ। और फिर निकालेंगे {ndate} के लेवल्स।"},
    "summary_review": {"en": "How NIFTY 50 traded on {review} around the Fibonacci pivot levels from {basis}, and the reference levels for {ndate}. Educational analysis — not a forecast or a recommendation.",
                       "hi": "{review} को NIFTY 50 ने {basis} के फिबोनाची पिवट लेवल्स के आसपास कैसे ट्रेड किया, और {ndate} के रेफरेंस लेवल्स। एजुकेशनल एनालिसिस — कोई प्रेडिक्शन या सलाह नहीं।"},
    "h_next": {"en": "Reference levels for {date}", "hi": "{date} के रेफरेंस लेवल्स"},
    "ch_next_levels": {"en": "Levels for the next session", "hi": "अगले सेशन के लेवल्स"},
    "c_next_levels": {"en": "Reference levels for {date}", "hi": "{date} के रेफरेंस लेवल्स"},
    "c_next_sub": {"en": "from the {date} high, low and close", "hi": "{date} के हाई, लो और क्लोज़ से"},
    "lim_no_open": {"en": "The source gives no opening price for {rdate}, so the opening gap is not discussed.",
                    "hi": "{rdate} का ओपनिंग प्राइस सोर्स में नहीं है, इसलिए ओपनिंग गैप की बात नहीं की गई।"},
    "lim_one_tf": {"en": "Only {tf} data was available, so the two timeframes are not compared.",
                   "hi": "केवल {tf} डेटा उपलब्ध था, इसलिए दोनों टाइमफ़्रेम की तुलना नहीं की गई।"},
    "lim_bars": {"en": "Each bar shows only its open, high, low and close; movement inside a bar is not visible.",
                 "hi": "हर बार केवल ओपन, हाई, लो और क्लोज़ दिखाता है; बार के भीतर की हलचल नहीं दिखती।"},
    "lim_incomplete": {"en": "The {tf} data for {date} is incomplete ({n} of {exp} bars), so it is not analysed.",
                       "hi": "{date} का {tf} डेटा अधूरा है ({exp} में से {n} बार), इसलिए इसका विश्लेषण नहीं किया गया।"},
    "lim_prev": {"en": "The previous session is not in the dataset, so the day's change is not stated.",
                 "hi": "डेटा में पिछला सत्र नहीं है, इसलिए दिन का बदलाव नहीं बताया गया।"},
    "lim_avg": {"en": "Fewer than 20 contiguous sessions are available, so the range is not compared with an average.",
                "hi": "लगातार 20 सत्र उपलब्ध नहीं हैं, इसलिए रेंज की तुलना औसत से नहीं की गई।"},
    "lim_order": {"en": "Daily bars show only the open, high, low and close, not the order in which levels were reached.",
                  "hi": "दैनिक बार केवल ओपन, हाई, लो और क्लोज़ दिखाते हैं — यह नहीं कि स्तर किस क्रम में छुए गए।"},
    "lim_intraday": {"en": "No verified 15-minute or 1-hour data was available for this session, so there is no intraday analysis.",
                     "hi": "इस सत्र के लिए सत्यापित 15-मिनट या 1-घंटे का डेटा उपलब्ध नहीं था, इसलिए कोई इंट्राडे विश्लेषण नहीं है।"},
    "lim_future": {"en": "The following session has not happened yet; levels are reference points only, not predictions.",
                   "hi": "अगला सत्र अभी हुआ नहीं है; ये स्तर केवल संदर्भ बिंदु हैं, भविष्यवाणी नहीं।"},
    "lim_nature": {"en": "Source data is {nature}, not live.", "hi": "स्रोत डेटा {nature} है, लाइव नहीं।"},
    "title": {"en": "NIFTY 50 Fibonacci Pivot Analysis | {date}", "hi": "NIFTY 50 फिबोनाची पिवट विश्लेषण | {date}"},
    "intro_hist": {
        "en": "This is a historical case study of the NIFTY 50 index. We calculate Fibonacci pivot levels from the {basis} "
              "session, using {nature} data, and then look at how the index actually traded on {review}.",
        "hi": "यह NIFTY 50 इंडेक्स की एक ऐतिहासिक केस स्टडी है। हम {basis} सत्र के {nature} डेटा से फिबोनाची पिवट स्तर "
              "निकालेंगे, और फिर देखेंगे कि {review} को इंडेक्स ने असल में कैसा कारोबार किया।"},
    "intro_daily": {
        "en": "This is an educational look at NIFTY 50 Fibonacci pivot levels calculated from the {basis} session, using "
              "{nature} data.",
        "hi": "यह {basis} सत्र से निकाले गए NIFTY 50 फिबोनाची पिवट स्तरों की एक शैक्षिक व्याख्या है, जिसमें {nature} "
              "डेटा का उपयोग किया गया है।"},
    "disclosure": {
        "en": "This content is for educational and informational purposes. It explains NIFTY 50 index-level technical "
              "analysis using {nature} data from {source} for the session of {date}. Technical levels are analytical "
              "reference points, not guarantees of future price movements. Market risks remain, and viewers should "
              "independently verify information before making financial decisions.",
        # Draft translation — must be reviewed before public use (rule DISCLOSURE-HI).
        "hi": "यह सामग्री केवल शैक्षिक और सूचनात्मक उद्देश्य के लिए है। इसमें {source} से लिए गए {date} सत्र के {nature} "
              "डेटा के आधार पर NIFTY 50 इंडेक्स-स्तर के तकनीकी विश्लेषण को समझाया गया है। तकनीकी स्तर केवल विश्लेषणात्मक "
              "संदर्भ बिंदु हैं, भविष्य की कीमतों की गारंटी नहीं। बाज़ार जोखिम बने रहते हैं, और कोई भी वित्तीय निर्णय लेने से "
              "पहले दर्शकों को जानकारी की स्वयं जाँच करनी चाहिए।"},
    "ribbon": {"en": "Educational content · NIFTY 50 {nature} data · session {date} · levels are reference points, not guarantees",
               "hi": "शैक्षिक सामग्री · NIFTY 50 {nature} डेटा · सत्र {date} · स्तर केवल संदर्भ बिंदु हैं, गारंटी नहीं"},
    "summary_hist": {"en": "Historical case study: Fibonacci pivot levels calculated from the NIFTY 50 session of {basis}, "
                           "and how the index traded on {review}.",
                     "hi": "ऐतिहासिक केस स्टडी: {basis} के NIFTY 50 सत्र से निकाले गए फिबोनाची पिवट स्तर, और {review} को "
                           "इंडेक्स का कारोबार।"},
    "summary_daily": {"en": "Fibonacci pivot reference levels calculated from the NIFTY 50 session of {basis}. Educational "
                            "explanation only.",
                      "hi": "{basis} के NIFTY 50 सत्र से निकाले गए फिबोनाची पिवट संदर्भ स्तर। केवल शैक्षिक व्याख्या।"},
    "h_data": {"en": "Data", "hi": "डेटा"},
    "d_index": {"en": "• Index: NIFTY 50 (NSE), daily OHLC, {nature} data — not live.",
                "hi": "• इंडेक्स: NIFTY 50 (NSE), दैनिक OHLC, {nature} डेटा — लाइव नहीं।"},
    "d_source": {"en": "• Source: {source}. Retrieved {retrieved}. Timezone: Asia/Kolkata.",
                 "hi": "• स्रोत: {source}। प्राप्त: {retrieved}। टाइमज़ोन: Asia/Kolkata।"},
    "d_session": {"en": "• Session {date}: Open {o} · High {h} · Low {l} · Close {c}",
                  "hi": "• सत्र {date}: ओपन {o} · हाई {h} · लो {l} · क्लोज़ {c}"},
    "d_following": {"en": "• Following session {date}: Open {o} · High {h} · Low {l} · Close {c}",
                    "hi": "• अगला सत्र {date}: ओपन {o} · हाई {h} · लो {l} · क्लोज़ {c}"},
    "h_levels": {"en": "Fibonacci pivot levels (from the {date} session)", "hi": "फिबोनाची पिवट स्तर ({date} सत्र से)"},
    "h_formula": {"en": "Formula", "hi": "सूत्र"},
    "h_showed": {"en": "What the data showed", "hi": "डेटा ने क्या दिखाया"},
    "h_limits": {"en": "Limitations", "hi": "सीमाएँ"},
    "h_chapters": {"en": "Chapters", "hi": "अध्याय"},
    "h_disclosure": {"en": "Disclosure", "hi": "डिस्क्लोज़र"},
    "credits": {"en": "The narration uses a synthetic (text-to-speech) voice. Charts are drawn by the channel from the "
                      "stated data.\nFonts: Noto Sans and Noto Sans Devanagari (SIL Open Font License). Voice model: {voice}.",
                "hi": "इस वीडियो में सिंथेटिक (टेक्स्ट-टू-स्पीच) आवाज़ का उपयोग किया गया है। चार्ट चैनल ने बताए गए डेटा से "
                      "बनाए हैं।\nफ़ॉन्ट: Noto Sans और Noto Sans Devanagari (SIL Open Font License)। वॉइस मॉडल: {voice}।"},
    "ch_intro": {"en": "Introduction", "hi": "शुरुआत"}, "ch_context": {"en": "The session", "hi": "सेशन"},
    "ch_calculation": {"en": "Calculating the pivots", "hi": "पिवट कैसे निकालते हैं"},
    "ch_levels": {"en": "What S2 and R2 mean", "hi": "S2 और R2 का मतलब"},
    "ch_review": {"en": "What happened next", "hi": "मार्केट ने क्या किया"},
    "ch_intraday_15m": {"en": "15-minute chart", "hi": "15-मिनट चार्ट"},
    "ch_intraday_1h": {"en": "1-hour chart", "hi": "1-घंटे का चार्ट"},
    "ch_mtf": {"en": "15-minute and 1-hour levels", "hi": "15-मिनट और 1-घंटे के लेवल्स"},
    "ch_scenarios": {"en": "Scenarios and uncertainty", "hi": "पॉसिबल सिनेरियो"},
    "ch_recap": {"en": "Recap", "hi": "रीकैप"},
    "ch_limitations": {"en": "Limitations", "hi": "लिमिटेशन्स"}, "ch_disclosure": {"en": "Disclosure", "hi": "डिस्क्लोज़र"},
    # charts
    "c_candles": {"en": "NIFTY 50 — daily candles", "hi": "NIFTY 50 — डेली कैंडल"},
    "c_basis": {"en": "basis session {date}", "hi": "बेस सेशन {date}"},
    "c_calc": {"en": "Fibonacci pivot calculation", "hi": "फिबोनाची पिवट कैलकुलेशन"},
    "c_range": {"en": "Range H − L = {v}", "hi": "रेंज H − L = {v}"},
    "c_levels": {"en": "Fibonacci pivot levels", "hi": "फिबोनाची पिवट लेवल्स"},
    "c_from": {"en": "from {date}", "hi": "{date} से"},
    "c_next": {"en": "What happened next", "hi": "मार्केट ने क्या किया"},
    "c_limits": {"en": "Limitations of this data", "hi": "इस डेटा की लिमिटेशन्स"},
    "c_disclosure": {"en": "Disclosure", "hi": "डिस्क्लोज़र"},
    "c_title2": {"en": "Fibonacci Pivot Analysis", "hi": "फिबोनाची पिवट एनालिसिस"},
    "c_reached": {"en": "reached", "hi": "टच हुआ"},
    "c_footer": {"en": "NIFTY 50 daily data ({nature}) · {source} · retrieved {retrieved}",
                 "hi": "NIFTY 50 डेली डेटा ({nature}) · {source} · डेटा लिया: {retrieved}"},
    "c_15m": {"en": "NIFTY 50 — 15-minute candles", "hi": "NIFTY 50 — 15-मिनट कैंडल"},
    "c_1h": {"en": "NIFTY 50 — 1-hour candles", "hi": "NIFTY 50 — 1-घंटे की कैंडल"},
    "c_session": {"en": "session {date} · times in IST", "hi": "सेशन {date} · टाइम IST में"},
    "c_mtf": {"en": "Daily vs 15-minute vs 1-hour levels", "hi": "डेली vs 15-मिनट vs 1-घंटे के लेवल्स"},
    "c_mtf_sub": {"en": "calculated at the {date} close", "hi": "{date} के क्लोज़ पर कैलकुलेशन"},
    "c_daily": {"en": "Daily", "hi": "डेली"},
    "zone_res": {"en": "RESISTANCE ZONE", "hi": "रेजिस्टेंस ज़ोन"},
    "card_res": {"en": "Resistance zone (R1–R2)", "hi": "रेजिस्टेंस ज़ोन (R1–R2)"},
    "card_sup": {"en": "Support zone (S1–S2)", "hi": "सपोर्ट ज़ोन (S1–S2)"},
    "card_piv": {"en": "Pivot (P)", "hi": "पिवट (P)"},
    "card_note": {"en": "Calculated reference levels only — not investment advice",
                  "hi": "सिर्फ़ गणना से बने रेफरेंस लेवल्स — निवेश सलाह नहीं"},
    "c_next_session": {"en": "NEXT SESSION", "hi": "अगला सत्र"},
    "zone_sup": {"en": "SUPPORT ZONE", "hi": "सपोर्ट ज़ोन"},
    "c_15min": {"en": "15-Min", "hi": "15-मिनट"}, "c_1hour": {"en": "1-Hour", "hi": "1-घंटा"},
    "c_mtf_note": {"en": "15-Min = last 25 fifteen-minute bars (one session) · 1-Hour = last 35 one-hour bars (about five sessions)",
                   "hi": "15-मिनट = आख़िरी 25 पंद्रह-मिनट बार (एक सत्र) · 1-घंटा = आख़िरी 35 एक-घंटे बार (लगभग पाँच सत्र)"},
    "c_scen": {"en": "How levels are commonly interpreted", "hi": "लेवल्स को आम तौर पर कैसे पढ़ते हैं"},
    "c_scen_lines": {"en": "Test and hold — price touches the level, no close beyond it|"
                           "Close beyond — bars close past the level|"
                           "Failed breakout — a close beyond, then a close back|"
                           "Concepts only — not predictions or instructions",
                     "hi": "टेस्ट एंड होल्ड — प्राइस लेवल छूता है, पार क्लोज़ नहीं|क्लोज़ बियॉन्ड — कैंडल लेवल के पार क्लोज़ होती है|फेल्ड ब्रेकआउट — पार क्लोज़, फिर वापसी|सिर्फ़ कॉन्सेप्ट — कोई प्रेडिक्शन या सलाह नहीं"},
    "c_recap": {"en": "Recap — what the data showed", "hi": "रीकैप — डेटा ने क्या दिखाया"},
    "c_intraday_footer": {"en": "NIFTY 50 {tf} data ({nature}) · {source} · retrieved {retrieved}",
                          "hi": "NIFTY 50 {tf} डेटा ({nature}) · {source} · डेटा लिया: {retrieved}"},
    "st_not_tested": {"en": "not tested", "hi": "टेस्ट नहीं हुआ"},
    "st_tested_held": {"en": "tested, no close beyond", "hi": "टेस्ट हुआ, पार क्लोज़ नहीं"},
    "st_closed_beyond": {"en": "closed beyond", "hi": "पार क्लोज़ हुआ"},
    "st_failed_breakout": {"en": "failed breakout", "hi": "फेल्ड ब्रेकआउट"},
    "st_reached": {"en": "reached", "hi": "टच हुआ"},
    "st_future": {"en": "reference level", "hi": "रेफरेंस लेवल"},
    "m_touch": {"en": "{name} touch", "hi": "{name} टच"},
    "m_beyond": {"en": "close beyond {name}", "hi": "{name} पार क्लोज़"},
    "m_back": {"en": "back inside", "hi": "वापसी"},
    "th_badge": {"en": "{date} ANALYSIS", "hi": "{date} का एनालिसिस"},
    "th_next": {"en": "LEVELS FOR {ndate}", "hi": "{ndate} के लेवल्स"},
    "th_held": {"en": "{name} TESTED", "hi": "{name} टेस्ट हुआ"},
    "th_failed": {"en": "FAILED BREAKOUT AT {name}", "hi": "{name} पर फेल्ड ब्रेकआउट"},
    "th_beyond": {"en": "CLOSE BEYOND {name}", "hi": "{name} के पार क्लोज़"},
    "th_untested": {"en": "S2 & R2 NOT TESTED", "hi": "S2 और R2 टेस्ट नहीं हुए"},
    "th_levels": {"en": "S2 · PIVOT · R2", "hi": "S2 · पिवट · R2"},
    "thumb_sub": {"en": "Fibonacci Pivots", "hi": "फिबोनाची पिवट"},
}


def voice_credit(voice: dict | None) -> str:
    """Name and licence of the narration voice actually used (attribution is required for CC BY voices)."""
    if not voice:
        from ..media import tts
        voice = tts.current_voice()
    lic = voice.get("license") or "licence not recorded"
    name = "Kokoro" if voice.get("engine") == "kokoro" else "Piper"
    return f"{name} {voice.get('voice_id', '')} ({lic})".replace("  ", " ")


def t(key: str, lang: str, **kw: object) -> str:
    return T[key][lang].format(**kw)
