Objective
I am building an Agentic Workflow for a Python-based pipeline that summarizes newsletters (Stratechery/Lenny's) and emails them via the Gmail API. I need to implement a "Reviewer Agent" (Editor-in-Chief) and an LLM-as-a-Judge evaluation suite using pytest.

Please read this specification and generate the necessary Python files, specifically the test_reviewer.py script and the skeleton for the reviewer.py agent.

1. Architectural Context

The Actor (summarizer.py): Drafts the initial summary.

The Critic (reviewer.py): Audits the draft against raw article text and YFinance data.

The Judge (test_reviewer.py): An LLM-as-a-Judge that grades the Critic using a static test bench to ensure it catches specific errors.

2. The Evaluation Framework
We are evaluating the Reviewer Agent using a deterministic approach.

Judge Model: GPT-4o-mini (or equivalent). temperature=0.

Scoring: Binary (1 for Pass, 0 for Fail).

Passing Criteria: The Reviewer must score a 1 on Defect Catch Rate (did it find the error?), False Positive Rate (did it hallucinate an error?), and Critique Actionability (did it tell the Summarizer exactly how to fix it?).

3. The Test Cases (The "Golden Dataset")
The test suite will iterate through 6 specific scenarios.

TC-01 | Private Company Rule: Injected fake financial data for a private company. Expected: FAIL. Demand removal of private financial metrics.

TC-02 | Tone & AI-isms: Injected generic filler (e.g., "In the ever-evolving landscape..."). Expected: FAIL. Flag the banned words and enforce a professional PM tone.

TC-03 | Length & Conciseness: Injected a bloated paragraph with 8 repetitive bullet points. Expected: FAIL. Demand compression to a strict maximum of 3 bullet points.

TC-04 | POV Misattribution: Swapped author's critical take with a quoted competitor's optimistic take. Expected: FAIL. Identify that the thesis belongs to the quoted entity, not the author.

TC-05 | Missing Guest Context: Introduced a guest speaker without their job profile. Expected: FAIL. Instruct the Summarizer to add the guest's title and current company.

TC-06 | The Clean Pass: Perfect, concise summary with accurate data and clear POVs. Expected: PASS. Do not hallucinate errors. Output the final HTML.

4. Required File Setup to Generate
Please draft the following:

tests/test_reviewer.py: The pytest file containing the 6 tests and the LLM Judge logic. It should load mocked data from local files rather than hitting live APIs.

src/agents/reviewer.py: The skeleton code and system prompt for the Reviewer Agent to handle these constraints.

Data Models: Any pydantic or data structures needed to pass the raw_article, yfinance_data, and draft_summary seamlessly between these components.