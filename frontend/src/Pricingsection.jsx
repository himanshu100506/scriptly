import { useState, useCallback } from "react";
import { useAuth } from "./AuthContext.jsx";
import "./PricingSection.css";

const RAZORPAY_CHECKOUT_SRC = "https://checkout.razorpay.com/v1/checkout.js";
const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function loadRazorpayScript() {
  return new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve();
    const script = document.createElement("script");
    script.src = RAZORPAY_CHECKOUT_SRC;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Couldn't load Razorpay checkout."));
    document.body.appendChild(script);
  });
}

export default function PricingSection({ onSubscribed, onClose, onRequireAuth }) {
  const { token, user } = useAuth();
  const [status, setStatus] = useState("idle"); // idle | loading | success | error
  const [errorMsg, setErrorMsg] = useState("");

  const handleSubscribe = useCallback(async () => {
    if (!user) {
      onRequireAuth?.();
      return;
    }

    setStatus("loading");
    setErrorMsg("");

    try {
      const res = await fetch(`${API_BASE}/api/subscriptions/create`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "Couldn't start the subscription.");
      }

      const { subscription_id, razorpay_key_id, user_email } = await res.json();

      await loadRazorpayScript();

      const rzp = new window.Razorpay({
        key: razorpay_key_id,
        subscription_id,
        name: "Scriptly",
        description: "Scriptly Pro — monthly",
        theme: { color: "#a3e635" },
        prefill: { email: user_email || user?.email },
        handler: function () {
          // Payment authorized on Razorpay's side. The webhook is the source
          // of truth for activation, so this just reflects success optimistically.
          setStatus("success");
          onSubscribed?.();
        },
        modal: {
          ondismiss: function () {
            setStatus("idle");
          },
        },
      });

      rzp.on("payment.failed", function () {
        setStatus("error");
        setErrorMsg("Payment failed. You can try again anytime.");
      });

      rzp.open();
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setErrorMsg(err.message || "Something went wrong. Try again.");
    }
  }, [token, user, onSubscribed, onRequireAuth]);

  return (
    <section className="pricing-section">
      <div className="pricing-card">
        {onClose && (
          <button className="pricing-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        )}
        <div className="pricing-eyebrow">SCRIPTLY PRO</div>
        <div className="pricing-amount">
          <span className="pricing-currency">₹</span>99
          <span className="pricing-period">/month</span>
        </div>
        <ul className="pricing-features">
          <li>Unlimited Reel &amp; YouTube transcripts</li>
          <li>Hindi, English &amp; Hinglish output</li>
          <li>Priority processing queue</li>
        </ul>

        <button
          className="pricing-cta"
          onClick={handleSubscribe}
          disabled={status === "loading" || status === "success"}
        >
          {status === "loading" && "Starting checkout…"}
          {status === "success" && "You're subscribed ✓"}
          {status === "idle" && "Subscribe"}
          {status === "error" && "Try again"}
        </button>

        {status === "error" && <p className="pricing-error">{errorMsg}</p>}
        <p className="pricing-note">Cancel anytime. Billed via UPI Autopay or card.</p>
      </div>
    </section>
  );
}