import json
import logging
import os
from decimal import Decimal, ROUND_HALF_UP, getcontext

import boto3

# Configure logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Set precision for Decimal operations (important for financial calculations)
getcontext().prec = 28  

# === Utility Functions ===

def D(v):  
    """
    Safely convert a value into a Decimal.
    Using string conversion avoids floating-point precision errors.
    """
    return Decimal(str(v))

def round2(x: Decimal) -> Decimal:
    """
    Round a Decimal value to 2 decimal places (financial style: HALF_UP).
    Example: 123.456 → 123.46
    """
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# === Business Logic ===

def compute_decision(payload: dict) -> str:
    """
    Compute the credit decision for a new loan application.

    Arguments:
    - payload: dictionary containing:
        * "loanApplication": dict with details of the new loan
        * "loanApplicationsApproved": list of dicts with approved loans (can be empty)

    Returns:
    - One of: "APPROVED", "REJECTED", "MANUAL_REVIEW"
    """

    # Extract loan application and approved loans list
    la = payload["loanApplication"]
    approved_list = payload.get("loanApplicationsApproved", [])

    # Input values (already include monthlyDebt calculation from loan microservice)
    base_salary = D(la["baseSalary"])
    amount      = D(la["amount"])
    monthly_new = D(la["monthlyDebt"])  

    # 1. Maximum allowed debt capacity = 35% of salary
    cap_max = round2(base_salary * D("0.35"))

    # 2. Current debt = sum of monthlyDebt from approved loans
    current_debt = round2(sum(D(approved["monthlyDebt"]) for approved in approved_list) if approved_list else D("0"))

    # 3. Available capacity = max capacity - current debt
    cap_disp = round2(cap_max - current_debt)

    # 4. Decision logic
    if monthly_new <= cap_disp:
        print(monthly_new, "<=", cap_disp)
        # Extra rule: if amount > 5 times salary → send to manual review
        if amount > cap_max):
            print(amount, ">", cap_max)
            return "MANUAL_REVIEW"
        return "APPROVED"
    else:
        print(monthly_new, ">", cap_disp)
        return "REJECTED"


def build_amortization_schedule_using_fixed_fee(amount: Decimal,
                                                term_months: int,
                                                annual_rate: Decimal,
                                                fixed_monthly_fee: Decimal):
    """
    Build amortization schedule when the monthly fee is already known (monthlyDebt).
    We split each month into interest and principal using the monthly rate.

    interest_m = balance * i_month
    principal_m = min(fee - interest_m, balance)
    balance = balance - principal_m
    """
    i_month = annual_rate / D(12) if annual_rate else D("0")
    balance = amount
    schedule = []

    for m in range(1, term_months + 1):
        interest = round2(balance * i_month)
        principal = fixed_monthly_fee - interest
        if principal < D("0"):
            principal = D("0")
        # last month fix to avoid negative balance due to rounding
        if principal > balance:
            principal = balance
        principal = round2(principal)
        balance = round2(balance - principal)

        schedule.append({
            "month": m,
            "interest": float(interest),
            "principal": float(principal),
            "remaining": float(balance),
        })

        if balance <= D("0.00"):
            break

    return schedule


# ========== Email (SES) ==========

def send_schedule_email_if_configured(to_email: str,
                                      decision: str,
                                      amount: Decimal,
                                      term: int,
                                      monthly_fee: Decimal,
                                      schedule: list):
    """
    Send the amortization schedule by email via AWS SES
    """
    ses = boto3.client("ses", region_name="us-east-2")

    subject = f"[CrediYa] Your loan decision: {decision}"
    header = [
        f"Decision: {decision.lower()}",
        f"Amount: {round2(amount)}",
        f"Term (months): {term}",
        f"Monthly fee: {round2(monthly_fee)}",
        "",
        "Installment plan (month; interest; principal; remaining):"
    ]
    rows = [f"{r['month']}; {r['interest']}; {r['principal']}; {r['remaining']}" for r in schedule]
    body_txt = "\n".join(header + rows)

    try:
        ses.send_email(
            Source="email",
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": body_txt, "Charset": "UTF-8"}
                }
            }
        )
        logger.info("SES email sent to %s", to_email)
    except Exception as e:
        # Best-effort: log and continue
        logger.warning("SES send failed: %s", e)


# === HTTP Response Helper ===

def _response(status: int, body: dict):
    """
    Standard API Gateway HTTP response.
    Includes CORS headers for browser compatibility.
    """
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST"
        },
        "body": json.dumps(body)
    }


# === AWS Lambda Handler ===

def lambda_handler(event, context):
    """
    AWS Lambda entry point.
    
    - Supports CORS preflight (OPTIONS).
    - Reads the loan application payload from the request body.
    - Calls `compute_decision` to calculate the result.
    - Returns the decision as JSON.
    """

    # Handle preflight OPTIONS request for CORS
    if event.get("httpMethod") == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        # Extract and parse request body
        raw_body = event.get("body", "")
        payload = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        # Compute credit decision
        decision = compute_decision(payload)

        # Build schedule for the email (only for the NEW loan)
        la = payload["loanApplication"]
        to_email = la.get("email")
        amount   = D(la["amount"])
        term     = int(la["term"])
        annual   = D(la.get("annualInterestRate", "0"))
        monthly_fee = D(la["monthlyDebt"])

        schedule = build_amortization_schedule_using_fixed_fee(
            amount=amount,
            term_months=term,
            annual_rate=annual,
            fixed_monthly_fee=monthly_fee
        )

        # Best-effort email
        send_schedule_email_if_configured(
            to_email=to_email,
            decision=decision,
            amount=amount,
            term=term,
            monthly_fee=monthly_fee,
            schedule=schedule
        )

        # Return decision
        return _response(200, {"status": decision})

    except (KeyError, TypeError, ValueError) as e:
        # Validation errors (missing fields, wrong types, etc.)
        logger.warning("Bad request: %s", e)
        return _response(400, {"error": "Bad Request", "details": str(e)})

    except Exception as e:
        # Unexpected server error
        logger.exception("Internal error")
        return _response(500, {"error": "Internal Error"})
