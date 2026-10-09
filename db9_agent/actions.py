"""Write-action tools for the support agent (cancel / return / change address).

Used by the eval harness (scripts/08_eval_rl_env.py). Ownership is enforced in code:
a tool only ever touches orders of the bound user_id. Policy decisions (is it still
returnable? which refund method? how much?) are left to the agent - that is what the
eval measures.
"""
from __future__ import annotations

import json
import time

from strands import tool

from .db import connect
from .tools import Tracer


def make_action_tools(user_id: str, dsn: str, tracer: Tracer):
    def traced(name: str, args: dict, fn):
        t0 = time.time()
        try:
            out = fn()
            tracer.log("tool", tool=name, args=args, ms=int((time.time() - t0) * 1000), ok=True)
            return out
        except Exception as e:
            tracer.log("tool", tool=name, args=args, ms=int((time.time() - t0) * 1000), ok=False, error=str(e))
            return f"ERROR: {e}"

    def own_order(c, order_id: str) -> dict:
        row = c.execute("SELECT * FROM orders WHERE order_id = %s AND user_id = %s", (order_id, user_id)).fetchone()
        if not row:
            raise PermissionError(f"order {order_id} does not belong to the current customer")
        return row

    @tool
    def get_customer() -> str:
        """Get the current customer's profile: name, membership tier, default address."""
        def run():
            with connect(dsn) as c:
                return json.dumps(c.execute("SELECT * FROM customers WHERE user_id = %s", (user_id,)).fetchone(),
                                  default=str)
        return traced("get_customer", {}, run)

    @tool
    def get_order(order_id: str) -> str:
        """Get one of the current customer's orders, including status, ship_address and delivered_at.

        Args:
            order_id: e.g. NG-1001
        """
        def run():
            with connect(dsn) as c:
                return json.dumps(own_order(c, order_id), default=str)
        return traced("get_order", {"order_id": order_id}, run)

    @tool
    def cancel_order(order_id: str) -> str:
        """Cancel a PROCESSING order of the current customer; refunds the full amount to the original payment method.

        Args:
            order_id: order to cancel
        """
        def run():
            with connect(dsn) as c:
                o = own_order(c, order_id)
                if o["status"] != "processing":
                    return f"ERROR: order is {o['status']}; only processing orders can be cancelled"
                c.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = %s", (order_id,))
                c.execute("INSERT INTO refunds (order_id, amount_usd, method, reason) VALUES (%s, %s, 'original_payment', 'cancelled')",
                          (order_id, o["amount_usd"]))
            return f"order {order_id} cancelled, {o['amount_usd']} USD refunded to original payment"
        return traced("cancel_order", {"order_id": order_id}, run)

    @tool
    def return_order(order_id: str, refund_method: str, refund_amount_usd: float, reason: str) -> str:
        """Process a return for a DELIVERED order and issue the refund. Only call when policy allows the return.

        Args:
            order_id: order being returned
            refund_method: "original_payment" or "store_credit"
            refund_amount_usd: refund amount after any restocking fee
            reason: short reason
        """
        def run():
            if refund_method not in ("original_payment", "store_credit"):
                return "ERROR: refund_method must be original_payment or store_credit"
            with connect(dsn) as c:
                o = own_order(c, order_id)
                if o["status"] != "delivered":
                    return f"ERROR: order is {o['status']}; only delivered orders can be returned"
                if not 0 < float(refund_amount_usd) <= float(o["amount_usd"]):
                    return "ERROR: refund amount must be between 0 and the order amount"
                c.execute("UPDATE orders SET status = 'returned' WHERE order_id = %s", (order_id,))
                c.execute("INSERT INTO refunds (order_id, amount_usd, method, reason) VALUES (%s, %s, %s, %s)",
                          (order_id, round(float(refund_amount_usd), 2), refund_method, reason))
            return f"return recorded for {order_id}: {refund_amount_usd} USD as {refund_method}"
        return traced("return_order", {"order_id": order_id, "method": refund_method, "amount": refund_amount_usd}, run)

    @tool
    def update_shipping_address(order_id: str, new_address: str) -> str:
        """Change the shipping address of one of the current customer's orders (only while processing).

        Args:
            order_id: order to change
            new_address: full new address
        """
        def run():
            with connect(dsn) as c:
                o = own_order(c, order_id)
                if o["status"] != "processing":
                    return f"ERROR: order is {o['status']}; address can only change while processing"
                c.execute("UPDATE orders SET ship_address = %s WHERE order_id = %s", (new_address, order_id))
            return f"shipping address of {order_id} updated"
        return traced("update_shipping_address", {"order_id": order_id}, run)

    return [get_customer, get_order, cancel_order, return_order, update_shipping_address]
