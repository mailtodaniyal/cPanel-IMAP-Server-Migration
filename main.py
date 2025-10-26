#!/usr/bin/env python3
import argparse,subprocess,shlex,sys,os,csv,json,time,imaplib,ssl,base64
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
import requests

def nowts():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def load_accounts_from_csv(path):
    out=[]
    with open(path,"r",newline="",encoding="utf-8") as f:
        r=csv.DictReader(f)
        for row in r:
            out.append(row)
    return out

def fetch_accounts_from_cpanel(host,root_token,domain=None,use_ssl=True,port=2087):
    proto="https" if use_ssl else "http"
    url=f"{proto}://{host}:{port}/execute/Email/list_pops"
    headers={"Authorization":f"whm {root_token}"}
    params={}
    if domain:
        params["domain"]=domain
    r=requests.get(url,headers=headers,params=params,verify=False,timeout=30)
    r.raise_for_status()
    j=r.json()
    if "data" in j:
        out=[]
        for item in j["data"]:
            out.append({"user":item.get("user"),"domain":item.get("domain"),"email":f'{item.get("user")}@{item.get("domain")}'})
        return out
    if "cpanelresult" in j and "data" in j["cpanelresult"]:
        out=[]
        for item in j["cpanelresult"]["data"]:
            out.append({"user":item.get("user"),"domain":item.get("domain"),"email":f'{item.get("user")}@{item.get("domain")}'})
        return out
    return []

def create_mailbox_on_destination(host,root_token,user,domain,password,quota_mb=1024,use_ssl=True,port=2087):
    proto="https" if use_ssl else "http"
    url=f"{proto}://{host}:{port}/json-api/cpanel"
    headers={"Authorization":f"whm {root_token}","Content-Type":"application/x-www-form-urlencoded"}
    params={"cpanel_jsonapi_user":"root","cpanel_jsonapi_module":"Email","cpanel_jsonapi_func":"addpop",
            "domain":domain,"email":user,"password":password,"quota":str(quota_mb)}
    r=requests.post(url,headers=headers,data=params,verify=False,timeout=30)
    r.raise_for_status()
    return r.json()

def set_mailbox_password(host,root_token,user,domain,password,use_ssl=True,port=2087):
    proto="https" if use_ssl else "http"
    url=f"{proto}://{host}:{port}/json-api/cpanel"
    headers={"Authorization":f"whm {root_token}","Content-Type":"application/x-www-form-urlencoded"}
    params={"cpanel_jsonapi_user":"root","cpanel_jsonapi_module":"Email","cpanel_jsonapi_func":"passwdpop",
            "domain":domain,"email":user,"password":password}
    r=requests.post(url,headers=headers,data=params,verify=False,timeout=30)
    r.raise_for_status()
    return r.json()

def imap_message_count(host,user,password,port=993,ssl_enable=True,folder=None,timeout=30):
    try:
        if ssl_enable:
            M=imaplib.IMAP4_SSL(host,port,ssl_context=ssl.create_default_context())
        else:
            M=imaplib.IMAP4(host,port)
        M.login(user,password)
        if folder:
            rv,dat=M.select(f'"{folder}"',readonly=True)
        else:
            rv,dat=M.select()
        if rv!="OK":
            M.logout()
            return None
        count=int(dat[0].decode()) if dat and dat[0] else 0
        M.logout()
        return count
    except Exception:
        return None

def list_mailboxes(host,user,password,port=993,ssl_enable=True):
    try:
        if ssl_enable:
            M=imaplib.IMAP4_SSL(host,port,ssl_context=ssl.create_default_context())
        else:
            M=imaplib.IMAP4(host,port)
        M.login(user,password)
        rv,mailboxes=M.list()
        out=[]
        if rv=="OK" and mailboxes:
            for m in mailboxes:
                s=m.decode()
                parts=s.split(' "/" ')
                if len(parts)>=2:
                    name=parts[-1].strip('"')
                else:
                    name=s
                out.append(name)
        M.logout()
        return out
    except Exception:
        return []

def run_imapsync(src_host,src_user,src_pass,dst_host,dst_user,dst_pass,opts_extra="",imap_port_src=993,imap_port_dst=993,ssl_src=True,ssl_dst=True,dry_run=False,timeout=3600):
    cmd=["imapsync","--host1",src_host,"--user1",src_user,"--password1",src_pass,"--port1",str(imap_port_src),
         "--host2",dst_host,"--user2",dst_user,"--password2",dst_pass,"--port2",str(imap_port_dst),
         "--syncinternaldates","--automap","--addheader","--subscribe","--nosyncacls","--skipsize"]
    if not ssl_src:
        cmd.extend(["--noauthmd5","--ssl1","--tls1"])
    if not ssl_dst:
        cmd.extend(["--ssl2","--tls2"])
    if dry_run:
        cmd.append("--dry")
    if opts_extra:
        cmd.extend(shlex.split(opts_extra))
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        out,err=proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out,err=proc.communicate()
        return 124,out,err
    return proc.returncode,out,err

def ensure_dir(p):
    os.makedirs(p,exist_ok=True)

def generate_password():
    return base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")

def verify_mailboxes_mirror(src_host,src_user,src_pass,dst_host,dst_user,dst_pass,imap_port_src=993,imap_port_dst=993,ssl=True):
    src_boxes=list_mailboxes(src_host,src_user,src_pass,port=imap_port_src,ssl_enable=ssl)
    dst_boxes=list_mailboxes(dst_host,dst_user,dst_pass,port=imap_port_dst,ssl_enable=ssl)
    unique=set(src_boxes)|set(dst_boxes)
    mismatches=[]
    for box in unique:
        sc=imap_message_count(src_host,src_user,src_pass,port=imap_port_src,ssl_enable=ssl,folder=box)
        dc=imap_message_count(dst_host,dst_user,dst_pass,port=imap_port_dst,ssl_enable=ssl,folder=box)
        if sc is None or dc is None or sc!=dc:
            mismatches.append({"folder":box,"src_count":sc,"dst_count":dc})
    return mismatches

def update_mx_record_via_whm(host,root_token,domain,new_mx,priority=0,use_ssl=True,port=2087):
    proto="https" if use_ssl else "http"
    url=f"{proto}://{host}:{port}/json-api/cpanel"
    headers={"Authorization":f"whm {root_token}","Content-Type":"application/x-www-form-urlencoded"}
    params={"cpanel_jsonapi_user":"root","cpanel_jsonapi_module":"ZoneEdit","cpanel_jsonapi_func":"edit_zone_record",
            "domain":domain,"name":domain,"type":"MX","txtdata":new_mx,"priority":str(priority)}
    r=requests.post(url,headers=headers,data=params,verify=False,timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--accounts-csv",help="CSV with columns: email,src_pass,dst_pass (optional),quota_mb (optional)")
    parser.add_argument("--src-cpanel-host",help="source cPanel host for API or direct IMAP host")
    parser.add_argument("--dst-cpanel-host",help="destination cPanel host for API or IMAP host")
    parser.add_argument("--src-whm-token",help="root token for source WHM (optional for listing)")
    parser.add_argument("--dst-whm-token",help="root token for destination WHM (required to create mailboxes)")
    parser.add_argument("--src-imap-port",type=int,default=993)
    parser.add_argument("--dst-imap-port",type=int,default=993)
    parser.add_argument("--concurrency",type=int,default=6)
    parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--output-dir",default="./migration_output")
    parser.add_argument("--imap-extra-opts",default="")
    parser.add_argument("--update-mx",action="store_true")
    parser.add_argument("--new-mx",help="new MX hostname to set")
    parser.add_argument("--domain-for-mx",help="domain to update MX for")
    parser.add_argument("--mx-priority",type=int,default=0)
    args=parser.parse_args()

    ensure_dir(args.output_dir)
    accounts=[]
    if args.accounts_csv:
        raw=load_accounts_from_csv(args.accounts_csv)
        for r in raw:
            email=r.get("email") or r.get("Email") or r.get("user")
            src_pass=r.get("src_pass") or r.get("password") or r.get("src_password")
            dst_pass=r.get("dst_pass") or r.get("dst_password") or ""
            quota=r.get("quota_mb") or r.get("quota") or ""
            accounts.append({"email":email,"src_pass":src_pass,"dst_pass":dst_pass,"quota":quota})
    elif args.src_whm_token and args.src_cpanel_host:
        fetched=fetch_accounts_from_cpanel(args.src_cpanel_host,args.src_whm_token)
        for f in fetched:
            accounts.append({"email":f["email"],"src_pass":"","dst_pass":"","quota":""})
    else:
        print("Provide --accounts-csv or source WHM token and host",file=sys.stderr);sys.exit(2)

    tasks=[]
    results=[]
    session_report={"started_at":nowts(),"accounts_total":len(accounts),"dry_run":bool(args.dry_run),"jobs":[]}
    def prepare_account(ac):
        email=ac["email"]
        if "@" not in email:
            return {"email":email,"status":"bad","reason":"invalid email"}
        user,domain=email.split("@",1)
        dst_pass=ac.get("dst_pass") or generate_password()
        return {"email":email,"user":user,"domain":domain,"src_pass":ac.get("src_pass") or "", "dst_pass":dst_pass,"quota":ac.get("quota") or "1024"}
    prepared=[prepare_account(a) for a in accounts]
    for p in prepared:
        session_report["jobs"].append({"email":p["email"],"status":"pending"})
    if args.dst_whm_token:
        for p in prepared:
            try:
                if not args.dry_run:
                    create_mailbox_on_destination(args.dst_cpanel_host,args.dst_whm_token,p["user"],p["domain"],p["dst_pass"],quota_mb=int(p["quota"] or 1024))
                session_report["jobs"]=[{**j} for j in session_report["jobs"]]
            except Exception as e:
                for j in session_report["jobs"]:
                    if j["email"]==p["email"]:
                        j["status"]="failed_create"
                        j["reason"]=str(e)
    def migrate_one(p):
        email=p["email"]
        src_user=email
        dst_user=email
        src_pass=p["src_pass"]
        dst_pass=p["dst_pass"]
        job={"email":email,"start":nowts(),"status":"started"}
        logfile=os.path.join(args.output_dir,f"{email.replace('@','__')}.log")
        with open(logfile,"w",encoding="utf-8") as lf:
            lf.write(f"START {nowts()}\n")
        rc,out,err=run_imapsync(args.src_cpanel_host,src_user,src_pass,args.dst_cpanel_host,dst_user,dst_pass,opts_extra=args.imap_extra_opts,imap_port_src=args.src_imap_port,imap_port_dst=args.dst_imap_port,dry_run=args.dry_run)
        with open(logfile,"a",encoding="utf-8") as lf:
            lf.write(f"RC:{rc}\n")
            lf.write("STDOUT\n")
            lf.write(out or "")
            lf.write("\nSTDERR\n")
            lf.write(err or "")
            lf.write("\nEND\n")
        job["end"]=nowts()
        job["rc"]=rc
        if rc==0:
            job["status"]="ok"
        else:
            job["status"]="error"
            job["error_excerpt"]=(err or "")[:1000]
        return job
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures={ex.submit(migrate_one,p):p for p in prepared}
        for fut in as_completed(futures):
            p=futures[fut]
            try:
                jobres=fut.result()
            except Exception as e:
                jobres={"email":p["email"],"status":"exception","error":str(e)}
            for j in session_report["jobs"]:
                if j["email"]==jobres.get("email"):
                    j.update(jobres)
            results.append(jobres)
    session_report["finished_at"]=nowts()
    session_report["results"]=results
    verification=[]
    for p in prepared:
        try:
            mism=verify_mailboxes_mirror(args.src_cpanel_host,p["email"],p["src_pass"],args.dst_cpanel_host,p["email"],p["dst_pass"],imap_port_src=args.src_imap_port,imap_port_dst=args.dst_imap_port,ssl=True)
            verification.append({"email":p["email"],"mismatches":mism})
        except Exception as e:
            verification.append({"email":p["email"],"error":str(e)})
    session_report["verification"]=verification
    if args.update_mx and args.dst_whm_token and args.domain_for_mx and args.new_mx:
        try:
            if not args.dry_run:
                mxres=update_mx_record_via_whm(args.dst_cpanel_host,args.dst_whm_token,args.domain_for_mx,args.new_mx,priority=args.mx_priority)
                session_report["mx_update"]=mxres
            else:
                session_report["mx_update"]="dry_run_no_change"
        except Exception as e:
            session_report["mx_update_error"]=str(e)
    outpath=os.path.join(args.output_dir,"migration_report.json")
    with open(outpath,"w",encoding="utf-8") as f:
        json.dump(session_report,f,indent=2)
    summary_lines=[]
    ok_count=sum(1 for r in results if r.get("status")=="ok")
    err_count=len(results)-ok_count
    summary_lines.append(f"Migration run {session_report['started_at']} -> {session_report['finished_at']}")
    summary_lines.append(f"Total accounts: {len(prepared)} OK: {ok_count} Errors: {err_count}")
    summary_lines.append(f"Report saved to: {outpath}")
    summary_lines.append("Rollback tip: point MX back to previous host, or restore mailbox data from source server backups. Keep source server online until rollback window expires.")
    for l in summary_lines:
        print(l)
    print("Detailed per-account logs and verification are in the output directory.")

if __name__=="__main__":
    main()
