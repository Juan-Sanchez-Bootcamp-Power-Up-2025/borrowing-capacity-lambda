import json
import logging
from decimal import Decimal, ROUND_HALF_UP, getcontext

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
    deuda_actual = round2(sum(D(approved["monthlyDebt"]) for approved in approved_list) if approved_list else D("0"))

    # 3. Available capacity = max capacity - current debt
    cap_disp = round2(cap_max - deuda_actual)

    # 4. Decision logic
    if monthly_new <= cap_disp:
        print(monthly_new, "<=" ,cap_disp)
        # Extra rule: if amount > 5 times salary → send to manual review
        if amount > base_salary * D(5):
            print(amount, ">" ,base_salary * D(5))
            return "MANUAL_REVIEW"
        return "APPROVED"
    else:
        print(monthly_new, ">" ,cap_disp)
        return "REJECTED"


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
