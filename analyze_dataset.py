import glob, json, os

history_dir = 'live_history_v2'
files = glob.glob(os.path.join(history_dir, '*.jsonl'))

total_files = len(files)
if total_files == 0:
    print('No files found.')
    exit()

files_with_closure = 0
files_without_closure = 0

total_snapshots = 0
corrupt_snapshots = 0
null_markets = 0

for fpath in files:
    has_closure = False
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                if not line.strip(): continue
                try:
                    record = json.loads(line)
                    rtype = record.get('record_type')
                    if rtype == 'match_closure':
                        has_closure = True
                    elif rtype == 'snapshot' or rtype == 'live_snapshot':
                        total_snapshots += 1
                        
                        used_mkts = record.get('used_markets', {})
                        if not used_mkts:
                            null_markets += 1
                            continue
                            
                        is_corrupt = False
                        for mkt_name, mkt_data in used_mkts.items():
                            if not mkt_data: continue
                            if not isinstance(mkt_data, dict): continue
                            over = mkt_data.get('over', 0)
                            under = mkt_data.get('under', 0)
                            if (over > 0 and over < 1.01) or (under > 0 and under < 1.01):
                                is_corrupt = True
                                break
                            if over > 200.0 or under > 200.0:
                                is_corrupt = True
                                break
                                
                        if is_corrupt:
                            corrupt_snapshots += 1
                            
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        pass
        
    if has_closure:
        files_with_closure += 1
    else:
        files_without_closure += 1

print(f'Total Matches (Files): {total_files}')
print(f'Matches WITH closure: {files_with_closure} ({files_with_closure/total_files*100:.1f}%)')
print(f'Matches WITHOUT closure: {files_without_closure} ({files_without_closure/total_files*100:.1f}%)')
print(f'Total Snapshots: {total_snapshots}')
if total_snapshots > 0:
    print(f'Corrupt Snapshots (Odds <1.01 or >200): {corrupt_snapshots} ({(corrupt_snapshots/total_snapshots)*100:.2f}%)')
    print(f'Snapshots with missing markets: {null_markets} ({(null_markets/total_snapshots)*100:.2f}%)')
