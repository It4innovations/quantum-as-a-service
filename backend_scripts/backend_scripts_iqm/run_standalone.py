import os
import glob
import json
import time
import csv
from pathlib import Path
from qiskit import QuantumCircuit
from qiskit.qasm2 import dump as dump_qasm2
import numpy as np
import matplotlib.pyplot as plt
from iqm.qiskit_iqm import IQMProvider
from iqm.qiskit_iqm import transpile_to_IQM

#------------------------------
# Setup QProvider and QBackend
#------------------------------
server_url = os.getenv('IQM_SERVER_URL')
provider = IQMProvider(server_url)
backend = provider.get_backend()
print(f'Qubit: {backend.architecture.qubits}')
print(f'Gates: {backend.architecture.gates.keys()}')

#--------------------------
# Setup input/output paths
#--------------------------
input_dir = ""
output_dir = "./output"

# Create output directory if it doesn't exist
os.makedirs(output_dir, exist_ok=True)

#--------------------------
# Find all .qasm2 files
#--------------------------
qasm_files = glob.glob(os.path.join(input_dir, "*.qasm2"))
if not qasm_files:
    print(f"No .qasm2 files found in {input_dir} directory")
    exit(1)

print(f"Found {len(qasm_files)} .qasm2 files to process")

# Get shots from environment
SHOTS = int(os.getenv('RUN_SHOTS', '1'))

# TODO: transpilation times
transpilation_times = np.array([0.0]*len(qasm_files),dtype=np.float32)
runtimes_times = np.array([0.0]*len(qasm_files),dtype=np.float32)

#--------------------------
# Process each QASM file
#--------------------------
for f_idx, qasm_file in enumerate(qasm_files):
    print(f"\n{'='*50}")
    print(f"Processing: {os.path.basename(qasm_file)}")
    print(f"{'='*50}")
    
    # Create base filename for outputs
    base_name = Path(qasm_file).stem
    
    try:
        #------------------------
        # Load quantum circuit
        #------------------------
        qc = QuantumCircuit.from_qasm_file(qasm_file)
        print(f"Circuit loaded: {qc.num_qubits} qubits, {qc.num_clbits} classical bits")
        
        # Save original circuit diagram
        try:
            fig = qc.draw(output='mpl')
            plt.savefig(os.path.join(output_dir, f"{base_name}_original_circuit.png"),
                       dpi=300, bbox_inches='tight')
            plt.close(fig)
        except Exception as e:
            print(f"Warning: Could not save original circuit diagram: {e}")
        
        if os.getenv('QC_TRANSPILED', "FALSE") == "TRUE":
            qc_transpiled = qc
        else:
            #-------------------
            # Transpile circuit
            #-------------------
            print("Transpiling circuit...")
            time_transpile = time.time()
            qc_transpiled = transpile_to_IQM(qc, backend, optimize_single_qubits=False)
            time_transpile = time.time() - time_transpile
            transpilation_times[f_idx] = time_transpile
            
        
        try:
            fig = qc_transpiled.draw(output='mpl')
            plt.savefig(os.path.join(output_dir, f"{base_name}_transpiled_circuit.png"),
                    dpi=300, bbox_inches='tight')
            plt.close(fig)
        except Exception as e:
            print(f"Warning: Could not save transpiled circuit diagram: {e}")
        
        # Save transpiled circuit qasm
        transpiled_qc_filename = os.path.join(output_dir, f"{base_name}_transpiled.qasm2")
        dump_qasm2(qc_transpiled, transpiled_qc_filename)
        
        #-------------
        # Run circuit
        #-------------
        print(f"Running circuit with {SHOTS} shots...")
        time_run = time.time()
        job = backend.run(qc_transpiled, shots=SHOTS)
        result = job.result()
        time_run = time.time() - time_run
        runtimes_times[f_idx] = time_run
        
        #--------------
        # Process results
        #--------------
        results_dict = result.get_counts()
        # print("Raw counts:", results_dict)
        
        # Prepare detailed results
        detailed_results = {
            "file": os.path.basename(qasm_file),
            "shots": SHOTS,
            "raw_counts": results_dict,
            "circuit_info": {
                "num_qubits": qc.num_qubits,
                "num_clbits": qc.num_clbits,
                "depth": qc.depth(),
                "gate_count": dict(qc.count_ops())
            }
        }
        
        non_zero_results = []
        # Collect non-zero results
        for key, count in results_dict.items():
            if count > 0:
                result_info = {
                    "state": key,
                    "counts": count,
                    "probability": count / SHOTS
                }
                
                # Only convert to int if key is actually a binary string
                if all(c in '01' for c in key):
                    decimal_value = int(key, 2)
                    result_info["decimal_value"] = decimal_value
                
                non_zero_results.append(result_info)
        # Measured shots
        measured_shots = np.atleast_2d(list(map(str, result.get_memory()))).tolist()

        #--------------
        # Save results
        #--------------
        # Save shots
        results_file = os.path.join(output_dir, f"{base_name}_results_shots.csv")
        with open(results_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(measured_shots)
        
        # Save results to CSV
        results_file = os.path.join(output_dir, f"{base_name}_results.csv")
        with open(results_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=non_zero_results[0].keys())
            writer.writeheader()
            writer.writerows(non_zero_results)
        
        # Save result details as JSON
        results_file = os.path.join(output_dir, f"{base_name}_results_metadata.json")
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(detailed_results, f, indent=2)
        
        # Save histogram plot
        if results_dict:
            try:
                states = list(results_dict.keys())
                counts = list(results_dict.values())

                fig, ax = plt.subplots(figsize=(max(6, len(states) * 0.5), 6))  # dynamic width
                ax.bar(states, counts)

                ax.set_title(f"Results for {base_name}")
                ax.set_xlabel("State")
                ax.set_ylabel("Counts")

                # Option 1: space labels (horizontal)
                ax.set_xticks(range(len(states)))
                ax.set_xticklabels(states, rotation=45, ha="right")

                # Option 2: vertical bars (better for many states)
                # ax.barh(states, counts)
                # ax.set_xlabel("Counts")
                # ax.set_ylabel("State")

                plt.tight_layout()
                plt.savefig(os.path.join(output_dir, f"{base_name}_histogram.png"),
                            dpi=300, bbox_inches="tight")
                plt.close(fig)

            except Exception as e:
                print(f"Warning: Could not save histogram: {e}")
        
        print(f"Results saved to output/ directory with prefix '{base_name}'")
        
    except Exception as e:
        print(f"Error processing {qasm_file}: {e}")
        # Save error information
        error_info = {
            "file": os.path.basename(qasm_file),
            "error": str(e),
            "error_type": type(e).__name__
        }
        error_file = os.path.join(output_dir, f"{base_name}_error.json")
        with open(error_file, 'w', encoding='utf-8') as f:
            json.dump(error_info, f, indent=2)

print(f"\n{'='*50}")
print("Batch processing completed!")
print(f"Results saved to: {output_dir}")
print(f"[AVG TRANSPILATION TIME]: {np.average(transpilation_times)}s")
print(f"[TOTAL TRANSPILATION TIME]: {np.sum(transpilation_times)}s")
print(f"[AVG RUNTIME]: {np.average(runtimes_times)}s")
print(f"[TOTAL RUNTIME]: {np.sum(runtimes_times)}s")
print(f"[TOTAL TIME]: {np.sum(runtimes_times)+np.sum(transpilation_times)}s")
print(f"{'='*50}")
