# ruff: noqa: E501 - message texts read better unwrapped
"""Warm-up email texts: natural business messages and short replies, varied with {spin|tax}.

They only ever go between our own mailboxes and our helper accounts. {{first_name}} is the recipient's
first name, {{sender_name}} the sender's display name. Mixed business topics, a few about property.
"""

import random

from app.services.templates import render

# (subject, body)
MESSAGES: list[tuple[str, str]] = [
    (
        "{Quick question|Question} about {next week|Thursday}",
        "{Hi|Hello} {{first_name|there}},\n\n{Do you have|Would you have} {a few minutes|time for a short call} "
        "{next week|on Thursday} to go over the {plan|numbers}? {Any time after 11 works for me|"
        "I'm free most afternoons}.\n\n{Thanks|Best},\n{{sender_name}}",
    ),
    (
        "{Meeting|Catch-up} {notes|summary}",
        "{Hi|Hello} {{first_name|there}},\n\n{Thanks for your time today|Good talking earlier}. "
        "{Here's a short summary|A quick recap}: we agreed to {share the draft by Friday|look at the figures again} "
        "and {meet again next month|check in after that}.\n\n{Let me know if I missed anything|Shout if anything "
        "looks off}.\n\n{{sender_name}}",
    ),
    (
        "{The document you asked for|Document as promised}",
        "{Hi|Hello} {{first_name|there}},\n\n{As promised|Following our chat}, I'll {send over|share} the "
        "{updated document|latest version} {later today|this afternoon}. {It has the changes we discussed|"
        "I've added the points from your notes}.\n\n{Best regards|Kind regards},\n{{sender_name}}",
    ),
    (
        "{Thanks|Thank you}!",
        "{Hi|Hello} {{first_name|there}},\n\n{Just wanted to say thanks|Many thanks} for {your help this week|"
        "sorting that out so quickly}. {Really appreciated|It made things much easier}.\n\n{{sender_name}}",
    ),
    (
        "{Following up|Quick follow-up}",
        "{Hi|Hello} {{first_name|there}},\n\n{Just following up|Circling back} on {my last email|our conversation} "
        "{about the timeline|about next steps}. {No rush|Whenever suits you}, {just let me know|"
        "a short reply is fine}.\n\n{Thanks|Cheers},\n{{sender_name}}",
    ),
    (
        "{Lunch|Coffee} {next week|on Tuesday}?",
        "{Hi|Hey} {{first_name|there}},\n\n{Are you around|Free} {next week|on Tuesday} for a quick "
        "{lunch|coffee}? {It's been a while|Would be good to catch up}.\n\n{{sender_name}}",
    ),
    (
        "{Updated|Revised} {schedule|timeline}",
        "{Hi|Hello} {{first_name|there}},\n\n{I've updated|I have revised} the {schedule|timeline} "
        "{after our call|based on your comments}. {The main change|The only change} is that "
        "{the review moves to Monday|we start a week later}.\n\n{Let me know if that works|Hope that's ok}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Viewing|Site visit} {on Saturday|this weekend}",
        "{Hi|Hello} {{first_name|there}},\n\n{The viewing|The site visit} is {confirmed|booked} for "
        "{Saturday at 11|Sunday morning}. {I'll meet you at the entrance|Parking is available on site}.\n\n"
        "{See you then|Thanks},\n{{sender_name}}",
    ),
    (
        "{Listing|Property} details",
        "{Hi|Hello} {{first_name|there}},\n\n{Here are the details|Quick note on the details} for the "
        "{apartment|property} we discussed: {three bedrooms, a balcony and parking|two bedrooms near the park}. "
        "{Happy to arrange a visit|Let me know if you'd like to see it}.\n\n{{sender_name}}",
    ),
    (
        "{Invoice|Payment} {question|query}",
        "{Hi|Hello} {{first_name|there}},\n\n{Could you check|Would you mind checking} whether {the last invoice|"
        "invoice 2041} {went through|was received}? {I don't see it on our side yet|Accounts asked me}.\n\n"
        "{Thanks|Many thanks},\n{{sender_name}}",
    ),
    (
        "{Next steps|Plan for next week}",
        "{Hi|Hello} {{first_name|there}},\n\n{For next week|Looking ahead}, I {suggest|think we should} "
        "{finish the review first|start with the budget}, then {set a date|decide on the rest}. "
        "{What do you think?|Does that sound right?}\n\n{{sender_name}}",
    ),
    (
        "{Call|Phone call} {tomorrow|later}",
        "{Hi|Hello} {{first_name|there}},\n\n{Is it ok if I call you|Can I give you a call} {tomorrow morning|"
        "later today}? {It won't take long|Only a few minutes}.\n\n{{sender_name}}",
    ),
    (
        "{Introduction|Quick introduction}",
        "{Hi|Hello} {{first_name|there}},\n\n{I'd like to introduce|Let me introduce} {a colleague|our new "
        "team member}, who will {help with the project|be your contact for the next few weeks}. "
        "{You'll hear from them soon|They'll be in touch shortly}.\n\n{Best|Regards},\n{{sender_name}}",
    ),
    (
        "{Price|Quote} {update|question}",
        "{Hi|Hello} {{first_name|there}},\n\n{Do you have|Could you send} {an updated quote|the latest prices} "
        "{for the order|for the next batch}? {We'd like to confirm this week|We're planning the budget}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Weekend plans|Good weekend}?",
        "{Hi|Hey} {{first_name|there}},\n\n{Hope you had|Hope you're having} a {good|nice} week. "
        "{Anything nice planned for the weekend?|Doing anything fun this weekend?}\n\n{{sender_name}}",
    ),
    (
        "{Contract|Agreement} {draft|review}",
        "{Hi|Hello} {{first_name|there}},\n\n{I've gone through|I reviewed} the {draft|agreement} and "
        "{only have two small comments|it looks good overall}. {I'll send them over shortly|"
        "Can we discuss tomorrow?}\n\n{{sender_name}}",
    ),
    (
        "{Availability|Your availability}",
        "{Hi|Hello} {{first_name|there}},\n\n{Could you share|What's} your availability {next week|for a short "
        "meeting}? {Mornings are best for me|I'm flexible}.\n\n{Thanks|Best},\n{{sender_name}}",
    ),
    (
        "{Report|Monthly report} {ready|attached soon}",
        "{Hi|Hello} {{first_name|there}},\n\n{The monthly report|This month's report} is {almost ready|nearly "
        "done}. {Numbers look good|We're slightly ahead of plan}; {I'll share it tomorrow|full version to "
        "follow}.\n\n{{sender_name}}",
    ),
    (
        "{Rental|Lease} {renewal|question}",
        "{Hi|Hello} {{first_name|there}},\n\n{The lease|The rental agreement} {is up for renewal|ends} "
        "{next month|in June}. {Would you like to extend|Shall we talk about renewing}?\n\n{{sender_name}}",
    ),
    (
        "{Feedback|Your thoughts}",
        "{Hi|Hello} {{first_name|there}},\n\n{I'd love your thoughts|Could I get your feedback} on {the "
        "proposal|the new layout} {when you have a moment|before Friday}? {Even a few lines helps|"
        "Short notes are fine}.\n\n{Thanks|Cheers},\n{{sender_name}}",
    ),
    (
        "{Delivery|Shipment} {update|date}",
        "{Hi|Hello} {{first_name|there}},\n\n{The delivery|Your order} {is scheduled for|should arrive on} "
        "{Wednesday|Thursday}. {I'll confirm the time|We'll send tracking details} {tomorrow|soon}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Workshop|Training} {next month|dates}",
        "{Hi|Hello} {{first_name|there}},\n\n{We're planning|We're organising} a {short workshop|training "
        "session} {next month|in the first week of the month}. {Would you like to join?|Let me know if you're "
        "interested}.\n\n{{sender_name}}",
    ),
    (
        "{Open house|Viewing day} {this week|on Friday}",
        "{Hi|Hello} {{first_name|there}},\n\n{We're holding|There's} an {open house|viewing day} {this Friday|"
        "on Saturday} {from 10 to 2|in the afternoon}. {Feel free to drop by|You're very welcome to come}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Reminder|Friendly reminder}",
        "{Hi|Hello} {{first_name|there}},\n\n{Just a reminder|A quick reminder} about {our meeting|the call} "
        "{tomorrow at 10|on Monday}. {See you then|Talk soon}.\n\n{{sender_name}}",
    ),
    (
        "{Office move|New office}",
        "{Hi|Hello} {{first_name|there}},\n\n{Good news|Quick update}: we're {moving to a new office|changing "
        "offices} {next month|at the end of the month}. {The new address follows soon|I'll share details "
        "shortly}.\n\n{{sender_name}}",
    ),
    (
        "{Budget|Numbers} for {Q3|next quarter}",
        "{Hi|Hello} {{first_name|there}},\n\n{I've put together|Here's a first draft of} the {budget|numbers} "
        "for {next quarter|Q3}. {Can we go through it together?|Let me know what you think.}\n\n{{sender_name}}",
    ),
    (
        "{Photos|Pictures} from {the visit|the site}",
        "{Hi|Hello} {{first_name|there}},\n\n{I took a few photos|Got some pictures} {during the visit|at the "
        "site} {today|yesterday}. {I'll send them over|Will share them} {this evening|shortly}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Holiday|Time off} {next week|coming up}",
        "{Hi|Hello} {{first_name|there}},\n\n{I'll be away|I'm off} {next week|from Monday to Wednesday}. "
        "{If anything urgent comes up|For anything urgent}, {just call me|please email the office}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Supplier|Vendor} {options|shortlist}",
        "{Hi|Hello} {{first_name|there}},\n\n{I've shortlisted|I found} {three suppliers|a few options} "
        "{for the project|that fit our budget}. {Want to compare them together?|I'll send the list today.}\n\n"
        "{{sender_name}}",
    ),
    (
        "{Congratulations|Well done}!",
        "{Hi|Hello} {{first_name|there}},\n\n{Congratulations on|Well done on} {the launch|closing the deal}! "
        "{Great work|Fantastic result}.\n\n{{sender_name}}",
    ),
    (
        "{Signed|Signature} {copy|needed}",
        "{Hi|Hello} {{first_name|there}},\n\n{Could you sign|Would you mind signing} {the form|page 3} and "
        "{send it back|return it} {by Thursday|this week}? {Thanks in advance|Much appreciated}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Market|Area} {update|news}",
        "{Hi|Hello} {{first_name|there}},\n\n{Prices in the area|The local market} {held steady|rose slightly} "
        "{this month|over the summer}. {Happy to share more detail|I can send the full numbers} if useful.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Website|Site} {changes|update}",
        "{Hi|Hello} {{first_name|there}},\n\n{The website changes|The updates to the site} {are live|went out "
        "today}. {Have a look when you can|Let me know if anything looks off}.\n\n{{sender_name}}",
    ),
    (
        "{Team lunch|Team dinner} {on Friday|next week}",
        "{Hi|Hey} {{first_name|there}},\n\n{We're doing|There's} a {team lunch|team dinner} {on Friday|next "
        "Thursday}. {Would be great if you could join|Hope you can make it}.\n\n{{sender_name}}",
    ),
    (
        "{Order|Booking} {confirmation|confirmed}",
        "{Hi|Hello} {{first_name|there}},\n\n{Your booking|The order} {is confirmed|went through} for "
        "{the 14th|next Monday}. {Let me know if you need to change anything|Any changes, just reply}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Draft|First draft} {for review|ready}",
        "{Hi|Hello} {{first_name|there}},\n\n{The first draft|A draft} {is ready|is done} {for your review|if "
        "you'd like to look}. {No hurry|Comments by Friday would be great}.\n\n{{sender_name}}",
    ),
    (
        "{Mortgage|Financing} {question|options}",
        "{Hi|Hello} {{first_name|there}},\n\n{I spoke to the bank|I checked with the lender} about {financing|"
        "the mortgage}. {They have a few options|Rates look reasonable}; {I'll explain on our call|"
        "details to follow}.\n\n{{sender_name}}",
    ),
    (
        "{Quick update|Status update}",
        "{Hi|Hello} {{first_name|there}},\n\n{Quick update|Just so you know}: {we're on track|everything is on "
        "schedule} {for the end of the month|for the deadline}. {More next week|I'll keep you posted}.\n\n"
        "{{sender_name}}",
    ),
    (
        "{Event|Conference} {next month|tickets}",
        "{Hi|Hello} {{first_name|there}},\n\n{Are you going to|Will you be at} the {conference|industry event} "
        "{next month|in the spring}? {Would be good to meet there|Maybe we can catch up}.\n\n{{sender_name}}",
    ),
    (
        "{Keys|Handover} {arrangement|on Monday}",
        "{Hi|Hello} {{first_name|there}},\n\n{The handover|Key collection} is {set for Monday|arranged for "
        "Tuesday} {at 9|in the morning}. {Bring your ID please|I'll have the paperwork ready}.\n\n"
        "{{sender_name}}",
    ),
]

REPLIES: list[str] = [
    "{Thanks|Thank you}, {{first_name|}}! {Sounds good|That works for me}.\n\n{{sender_name}}",
    "{Got it|Noted}, {thanks|thank you}. {I'll take a look|I'll get back to you} {today|tomorrow}.\n\n"
    "{{sender_name}}",
    "{Great|Perfect}, {see you then|talk then}.\n\n{{sender_name}}",
    "{Thanks for the update|Thanks for letting me know}. {All clear on my side|No questions from me}.\n\n"
    "{{sender_name}}",
    "{Yes|Sure}, {that works|that's fine}. {Thursday is good|Morning is best}.\n\n{{sender_name}}",
    "{Thanks|Many thanks}! {Looks good|Looking good}.\n\n{{sender_name}}",
    "{Sounds great|Lovely}, {count me in|I'll be there}.\n\n{{sender_name}}",
    "{Received|Got it}, {thank you|thanks}. {Will review shortly|I'll read it this afternoon}.\n\n"
    "{{sender_name}}",
    "{Appreciate it|Thanks a lot}. {Let's speak soon|Talk soon}.\n\n{{sender_name}}",
    "{No problem|Of course}. {Happy to help|Anytime}.\n\n{{sender_name}}",
    "{Thanks|Cheers}, {I'll confirm by tomorrow|I'll check and come back to you}.\n\n{{sender_name}}",
    "{Good to hear|Glad to hear it}. {Thanks for sharing|Thanks for the heads-up}.\n\n{{sender_name}}",
]


def first_name(display_name: str) -> str:
    return (display_name or "").strip().split(" ")[0]


def message(rng: random.Random, sender_name: str, recipient_name: str) -> tuple[str, str]:
    """A warm-up email (subject, plain-text body)."""
    subject, body = rng.choice(MESSAGES)
    fields = {"first_name": first_name(recipient_name), "sender_name": sender_name}
    return " ".join(render(subject, fields, rng).split()), render(body, fields, rng).rstrip() + "\n"


def reply(rng: random.Random, sender_name: str, recipient_name: str) -> str:
    """A short, natural answer to a warm-up email."""
    fields = {"first_name": first_name(recipient_name), "sender_name": sender_name}
    return render(rng.choice(REPLIES), fields, rng).replace(", !", "!").rstrip() + "\n"
