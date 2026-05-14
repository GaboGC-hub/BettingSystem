import json
import csv
import glob

def generar_reporte():
    archivos = glob.glob("[LOSS]*.jsonl") + glob.glob("[WIN]*.jsonl")
    
    with open('trade_blotter.csv', 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        # Encabezados del Excel
        writer.writerow(['Partido', 'Minuto', 'Mercado', 'Decision', 'Linea', 'Cuota', 'Stake', 'Profit', 'Resultado'])
        
        for archivo in archivos:
            with open(archivo, 'r', encoding='utf-8') as f:
                for linea in f:
                    data = json.loads(linea)
                    if data['record_type'] == 'match_closure':
                        partido = f"{data['match']['home_team']} vs {data['match']['away_team']}"
                        
                        # Extraer cada apuesta hecha
                        for snapshot in data.get('settled_snapshots', []):
                            minuto = snapshot['minute']
                            for mercado, apuesta in snapshot.get('settlements', {}).items():
                                writer.writerow([
                                    partido, 
                                    minuto, 
                                    mercado, 
                                    apuesta['decision'], 
                                    apuesta['linea'], 
                                    apuesta.get('stake', 0), 
                                    apuesta.get('profit', 0), 
                                    apuesta['resultado']
                                ])
    print("✅ Reporte Excel generado: trade_blotter.csv")

if __name__ == "__main__":
    generar_reporte()