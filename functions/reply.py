import smtplib
import base64
from flask import jsonify
import re
import socket
import email.utils
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import logging
import ssl

def reply(user_email, oauth_token, to_email, subject, body, reply, cc=None, bcc=None, smtp_server="smtp.gmail.com", smtp_port=587):
    """
    Sends an email using OAuth 2.0 authentication with smtplib.
    Handles non-ASCII characters and provides robust error handling.
    
    :param user_email: Sender's email address
    :param oauth_token: OAuth 2.0 access token
    :param to_email: Recipient's email address
    :param subject: Email subject
    :param body: Email body (can contain non-ASCII characters)
    :param cc: List of CC emails (optional)
    :param bcc: List of BCC emails (optional)
    :param smtp_server: SMTP server address (default: Gmail)
    :param smtp_port: SMTP server port (default: 587)
    :return: JSON response indicating success or failure
    """
    try:
        # Validate and clean email addresses
        def clean_address(addr):
            if not addr:
                return None
            email_match = re.search(r'<(.*?)>', addr)
            return email_match.group(1) if email_match else addr
        
        clean_email = clean_address(to_email)
        if not clean_email:
            return jsonify({'success': False, 'error': 'Invalid recipient email address'})
        
        # Create proper MIME message to handle non-ASCII characters
        msg = MIMEMultipart()
        msg['From'] = user_email
        msg['To'] = clean_email
        if reply:
            msg['Subject'] = Header(f"Re: {subject}", 'utf-8')
        else:
            msg['Subject'] = Header(f"{subject}", 'utf-8')
        msg['Date'] = email.utils.formatdate(localtime=True)
        
        # Add CC and BCC recipients if provided
        if cc:
            cc_list = [clean_address(addr) for addr in (cc if isinstance(cc, list) else [cc])]
            cc_list = [addr for addr in cc_list if addr]  # Filter out None values
            if cc_list:
                msg['Cc'] = ', '.join(cc_list)
        
        # Add body with proper encoding
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # Format all recipients for sendmail
        recipients = [clean_email]
        if cc:
            recipients.extend([clean_address(addr) for addr in (cc if isinstance(cc, list) else [cc]) if clean_address(addr)])
        if bcc:
            recipients.extend([clean_address(addr) for addr in (bcc if isinstance(bcc, list) else [bcc]) if clean_address(addr)])
        
        # Filter out any None or empty values
        recipients = [r for r in recipients if r]
        
        if not recipients:
            return jsonify({'success': False, 'error': 'No valid recipients provided'})
        
        # Format the OAuth2 authentication string
        auth_string = f"user={user_email}\x01auth=Bearer {oauth_token}\x01\x01"
        auth_string = base64.b64encode(auth_string.encode()).decode()
        
        # Resolve SMTP server IP to avoid slow DNS lookup
        try:
            smtp_ip = socket.gethostbyname(smtp_server)
            logging.info(f"Resolved {smtp_server} to {smtp_ip}")
        except socket.gaierror as e:
            logging.error(f"DNS resolution failed: {e}")
            return jsonify({'success': False, 'error': f'Cannot resolve SMTP server: {str(e)}'})
        
        # Initialize SMTP connection with improved error handling
        try:
            server = smtplib.SMTP(smtp_server, smtp_port, local_hostname=None, timeout=30)
            server.set_debuglevel(0)  # Set to 1 for detailed debugging
            
            # Identify ourselves to the server
            server.ehlo_or_helo_if_needed()
            
            # Check for TLS support
            if server.has_extn('STARTTLS'):
                try:
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()  # Re-identify after STARTTLS
                except (ssl.SSLError, smtplib.SMTPException) as e:
                    logging.error(f"TLS negotiation failed: {e}")
                    return jsonify({'success': False, 'error': f'TLS negotiation failed: {str(e)}'})
            else:
                logging.warning("STARTTLS not supported by server")
                
            # Authenticate via OAuth
            try:
                server.docmd("AUTH", "XOAUTH2 " + auth_string)
            except smtplib.SMTPAuthenticationError as e:
                logging.error(f"Authentication failed: {e}")
                return jsonify({'success': False, 'error': f'Authentication failed: {str(e)}'})
            
            # Send the email
            try:
                server.send_message(msg)
                logging.info("Email sent successfully")
            except smtplib.SMTPRecipientsRefused as e:
                logging.error(f"Recipients refused: {e}")
                return jsonify({'success': False, 'error': f'Recipients refused: {str(e)}'})
            except smtplib.SMTPDataError as e:
                logging.error(f"Data error: {e}")
                return jsonify({'success': False, 'error': f'Data error: {str(e)}'})
            
            # Quit the server
            server.quit()
            return jsonify({'success': True})
            
        except smtplib.SMTPConnectError as e:
            logging.error(f"Connection error: {e}")
            return jsonify({'success': False, 'error': f'Connection error: {str(e)}'})
        except socket.timeout as e:
            logging.error(f"Connection timeout: {e}")
            return jsonify({'success': False, 'error': f'Connection timeout: {str(e)}'})
        except socket.error as e:
            logging.error(f"Socket error: {e}")
            return jsonify({'success': False, 'error': f'Socket error: {str(e)}'})
            
    except Exception as e:
        logging.exception("Unexpected error")
        return jsonify({'success': False, 'error': f'An unexpected error occurred: {str(e)}'})