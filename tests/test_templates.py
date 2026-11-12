import random

from app.services.templates import compose, fill, footer, spin, variables_used


def test_spintax_picks_one_option_and_nests():
    rng = random.Random(1)
    seen = {spin("{Hi|Hello|Hey} there", rng) for _ in range(50)}
    assert seen == {"Hi there", "Hello there", "Hey there"}
    nested = {spin("{a|{b|c}}", rng) for _ in range(50)}
    assert nested == {"a", "b", "c"}


def test_spintax_leaves_variables_and_plain_braces_alone():
    rng = random.Random(1)
    assert spin("{{first_name|there}} {x}", rng) == "{{first_name|there}} {x}"


def test_variables_use_values_then_fallbacks():
    text = "Hi {{first_name|there}}, {{ company }} {{missing}}."
    assert fill(text, {"first_name": "Sara", "company": "Acme"}) == "Hi Sara, Acme ."
    assert fill(text, {"first_name": ""}) == "Hi there,  ."
    assert variables_used(text, "{{industry}}") == {"first_name", "company", "missing", "industry"}


def test_compose_adds_the_footer_and_sender_name():
    subject, body, html = compose(
        "{Quick|Short} question for {{company}}",
        "Hi {{first_name|there}},\n\nThanks,\n{{sender_name}}\n\n",
        {"company": "Acme"},
        "Ali Khan",
        footer("Ali Khan", "FastNexa", "1 Main St, Lahore"),
        random.Random(2),
    )
    assert subject in {"Quick question for Acme", "Short question for Acme"}
    assert body.startswith("Hi there,\n\nThanks,\nAli Khan\n\n--\nAli Khan · FastNexa\n1 Main St, Lahore\n")
    assert "reply and I won't email again" in body
    assert html is None  # plain-text campaign
