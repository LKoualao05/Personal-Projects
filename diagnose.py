#!/usr/bin/env python3
"""
Diagnostic script to see what emails are being found but not classified as confirmations
"""

import json
from main import load_config, get_creds, build_services, search_confirmation_messages, get_header, parse_from_header
from gmail_search import contains_confirmation

def diagnose_emails(limit=10):
    """Check first few emails to see why they're not being classified as confirmations"""
    cfg = load_config()
    creds = get_creds()
    gmail, sheets = build_services(creds)
    
    # Get potential message IDs
    ids = search_confirmation_messages(gmail, cfg["GMAIL_QUERY_WINDOW_DAYS"])
    print(f"Found {len(ids)} potential messages")
    
    print("\n=== DIAGNOSTIC: First {limit} messages ===")
    
    for i, mid in enumerate(ids[:limit]):
        try:
            msg = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject","From"]).execute()
            subject = get_header(msg, "Subject")
            from_header = get_header(msg, "From")
            from_name = parse_from_header(from_header)
            snippet = msg.get("snippet","")
            
            is_confirmation = contains_confirmation(subject, snippet)
            
            print(f"\n--- Message {i+1} ---")
            print(f"From: {from_name} ({from_header})")
            print(f"Subject: {subject}")
            print(f"Snippet: {snippet[:100]}...")
            print(f"Is Confirmation: {is_confirmation}")
            print("-" * 50)
            
        except Exception as e:
            print(f"Error processing message {i+1}: {e}")
            continue

if __name__ == "__main__":
    diagnose_emails(10)
