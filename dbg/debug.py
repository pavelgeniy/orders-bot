import urllib.request, re
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
urls=["https://www.peopleperhour.com/freelance-jobs/marketing-branding-sales","https://www.peopleperhour.com/freelance-jobs?q=seo",
"https://useme.com/pl/jobs/","https://useme.com/en/jobs/",
"https://www.weblancer.net/freelance/","https://www.weblancer.net/rss/",
"https://freelancehunt.com/projects","https://freelancehunt.com/projects.rss","https://freelancehunt.com/rss/projects",
"https://www.reddit.com/r/forhire+hiring+slavelabour/new/.rss","https://old.reddit.com/r/forhire/new/.rss",
"https://www.guru.com/d/jobs/","https://kabanchik.ua/ua/category/internet-marketing"]
for u in urls:
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9"}),timeout=30)
        b=r.read().decode("utf-8","replace")
        print("=====",u,r.status,len(b))
        hrefs=re.findall(r'href="([^"]+)"',b)
        print("HREFS:", [h for h in hrefs if re.search(r'\d{4,}',h)][:25])
        print("NEXT:", "__NEXT_DATA__" in b, "| sample:", re.sub(r"\s+"," ",b[:300]))
    except Exception as e:
        print("=====",u,"ERR",e)
