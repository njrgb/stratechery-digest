import sys
import os
import json
from openai import OpenAI
from google import genai

# Import shared logic from summarize.py (also sets up sys.stdout encoding)
from summarize import (
    fetch_latest,
    extract_companies,
    fetch_financials,
    format_financial_block,
    SUMMARIZE_PROMPT,
)


def summarize_openai(title, text, financial_block, client):
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": SUMMARIZE_PROMPT.format(
                title=title,
                text=text,
                financial_block=financial_block
            )
        }]
    )
    return response.choices[0].message.content


def summarize_gemini(title, text, financial_block, client):
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=SUMMARIZE_PROMPT.format(
            title=title,
            text=text,
            financial_block=financial_block
        )
    )
    return response.text


def print_section(label, model_name, summary, title, published, link):
    divider = "━" * 60
    print(f"\n{'═' * 60}")
    print(f"  {label}: {model_name}")
    print(f"{'═' * 60}")
    print(f"\n{divider}")
    print(f"STRATECHERY  |  {published[:16]}")
    print(f"\n{title}\n")
    print(divider)
    print(summary)
    print(f"\n{divider}")
    print(f"Read: {link[:80]}")
    print(divider)


if __name__ == "__main__":
    openai_key = os.environ.get('OPENAI_API_KEY')
    google_key = os.environ.get('GOOGLE_API_KEY')

    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")
    if not google_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable not set")

    openai_client = OpenAI(api_key=openai_key)
    gemini_client = genai.Client(api_key=google_key)

    print("Fetching latest Stratechery article...")
    title, published, link, text = fetch_latest()
    print(f"Found: {title} ({len(text)} chars)")

    print("Extracting public companies (using GPT-4o)...")
    companies = extract_companies(title, text, openai_client)
    print(f"Companies identified: {[c['ticker'] for c in companies] or 'none'}")

    financial_block = ""
    if companies:
        print("Fetching financials from Yahoo Finance...")
        financials = fetch_financials(companies)
        financial_block = format_financial_block(financials)

    print("\nSummarizing with GPT-4o...")
    summary_openai = summarize_openai(title, text, financial_block, openai_client)

    print("Summarizing with Gemini 2.0 Flash...")
    summary_gemini = summarize_gemini(title, text, financial_block, gemini_client)

    print_section("MODEL 1", "GPT-4o", summary_openai, title, published, link)
    print_section("MODEL 2", "Gemini 2.0 Flash", summary_gemini, title, published, link)
