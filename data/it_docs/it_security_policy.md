# IT Security Policy

**Document:** IT Security Policy  
**Effective Date:** 2024-01-15  
**Version:** 4.2  
**Owner:** IT Operations & Information Security  

---

## 1. Purpose and Scope

This policy establishes the requirements for protecting Acme Corp's information systems, networks, and data from unauthorized access, disclosure, modification, or destruction. It applies to all employees, contractors, vendors, and third parties who access Acme Corp technology resources.

This is an IT and Information Security policy, not an HR policy. Questions about this policy should be directed to the IT Security team at security@acmecorp.com, not HR.

---

## 2. Password and Authentication Requirements

All accounts with access to Acme Corp systems must comply with the following authentication standards:

### 2.1 Password Complexity and Rotation
User account passwords must:
- Be at least **16 characters** in length.
- Contain a mix of uppercase letters, lowercase letters, numbers, and special characters.
- Not include the user's name, username, or common dictionary words.
- **Be rotated every 90 days** for privileged accounts (system administrators, service accounts, API credentials).
- **Be rotated every 180 days** for standard user accounts.

Passwords must not be reused within the last 12 previous passwords. The use of a company-approved password manager (currently 1Password Enterprise) is mandatory for all employees.

### 2.2 Multi-Factor Authentication (MFA)
MFA is required for all access to:
- Company email (Google Workspace).
- Cloud infrastructure (AWS console, GCP console).
- VPN gateway (see Section 3).
- Any application handling Sensitive or Restricted data (see Data Classification Policy).

Acceptable MFA methods: hardware security key (FIDO2, preferred), authenticator app (TOTP), or SMS one-time code (least preferred; only permitted where hardware key and app are unavailable).

---

## 3. VPN and Remote Access

All access to internal Acme Corp systems (internal applications, databases, development environments) from outside the corporate network must be conducted over the company-approved VPN (Tailscale Mesh VPN). Employees must install and activate the VPN client before accessing any internal resource remotely.

**Split tunneling is disabled** on the corporate VPN profile to ensure all traffic is inspected. Personal internet traffic is not routed through the corporate VPN; a separate "personal" profile is available for use during personal browsing.

VPN credentials must not be shared with any other person, including colleagues. If a colleague requires remote access, they must request their own VPN profile through the IT helpdesk portal.

### 3.1 VPN Access Provisioning
New VPN access requires:
1. Manager approval submitted via the IT helpdesk portal.
2. Completion of the Remote Access Security Training module in the LMS.
3. MFA enrollment on the account that will authenticate the VPN session.

VPN profiles are reviewed quarterly. Accounts inactive for 90+ consecutive days are automatically disabled and require re-provisioning.

---

## 4. Device Management and Endpoint Security

All devices used to access Acme Corp systems must be enrolled in the company's Mobile Device Management (MDM) platform (Jamf for macOS/iOS, Microsoft Intune for Windows/Android). Personal devices may only be used under the BYOD program, which requires MDM enrollment and acceptance of a usage policy.

Enrolled devices must have:
- Full-disk encryption enabled (FileVault for Mac, BitLocker for Windows).
- Automatic OS security updates enabled.
- Endpoint Detection and Response (EDR) agent (CrowdStrike Falcon) installed and active.
- Screen lock activating after 5 minutes of inactivity.

Devices that are lost or stolen must be reported to the IT Security team within **2 hours** of discovery. Remote wipe will be initiated immediately upon report.

---

## 5. Data Handling and Classification

Acme Corp classifies data into four tiers: Public, Internal, Sensitive, and Restricted. Handling requirements scale with classification.

- **Restricted data** (PII, financial records, source code, credentials) must be stored only in approved, encrypted systems. It must not be emailed unencrypted, stored on personal devices, or transferred to unauthorized cloud storage.
- **Sensitive data** requires encryption in transit and at rest and access logging.
- All data handling must comply with applicable privacy regulations (GDPR, CCPA, SOC 2 controls).

Employees who become aware of a data breach, unauthorized access, or suspected compromise of any company system must report it to the IT Security team immediately at security@acmecorp.com or via the #security-incidents Slack channel.

---

## 6. Acceptable Use

Company IT resources (devices, email, internet, cloud storage) are provided for business purposes. Incidental personal use is permissible provided it does not consume significant bandwidth, violate any provision of this policy, or involve inappropriate content.

Prohibited activities include but are not limited to: installing unlicensed software, visiting known malicious websites, using company resources for personal commercial activity, attempting to bypass security controls, and sharing credentials.

Violations of this policy may result in immediate suspension of system access and disciplinary action up to and including termination of employment.
