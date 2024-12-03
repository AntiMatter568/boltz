from dataclasses import asdict, replace
import json
from pathlib import Path
from typing import Literal

import numpy as np
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import BasePredictionWriter
import torch
from torch import Tensor

from boltz.data.types import (
    Interface,
    Record,
    Structure,
)
from boltz.data.write.mmcif import to_mmcif
from boltz.data.write.pdb import to_pdb


class BoltzWriter(BasePredictionWriter):
    """Custom writer for predictions."""

    def __init__(
        self,
        data_dir: str,
        output_dir: str,
        output_format: Literal["pdb", "mmcif"] = "mmcif",
    ) -> None:
        """Initialize the writer.

        Parameters
        ----------
        output_dir : str
            The directory to save the predictions.

        """
        super().__init__(write_interval="batch")
        if output_format not in ["pdb", "mmcif"]:
            msg = f"Invalid output format: {output_format}"
            raise ValueError(msg)

        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir)
        self.output_format = output_format
        self.failed = 0

        # Create the output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_on_batch_end(
        self,
        trainer: Trainer,
        pl_module: LightningModule,
        prediction: dict[str, Tensor],
        batch_indices: list[int],
        batch: dict[str, Tensor],
        batch_idx: int,
        dataloader_idx: int,
    ) -> None:
        if prediction["exception"]:
            self.failed += 1
            return

        # Get the records
        records: list[Record] = batch["record"]

        # Get the predictions
        coords = prediction["coords"]
        coords = coords.unsqueeze(0)
        pad_masks = prediction["masks"]

        # Get ranking
        argsort = torch.argsort(prediction["confidence_score"], descending=True)
        idx_to_rank = {idx.item(): rank for rank, idx in enumerate(argsort)}

        # Iterate over the records
        for record, coord, pad_mask in zip(records, coords, pad_masks):
            # Load the structure
            path = self.data_dir / f"{record.id}.npz"
            structure: Structure = Structure.load(path)

            # Compute chain map with masked removed
            chain_map = {}
            for i, mask in enumerate(structure.mask):
                if mask:
                    chain_map[len(chain_map)] = i

            # Remove masked chains completely
            structure = structure.remove_invalid_chains()

            for model_idx in range(coord.shape[0]):
                # Save intermediate structures if available
                if "intermediate_coords" in prediction:
                    intermediate_dir = self.output_dir / record.id / "intermediate"
                    intermediate_dir.mkdir(exist_ok=True, parents=True)
                    
                    for step, step_coords in enumerate(prediction["intermediate_coords"]):
                        # Get coordinates for current model and step
                        model_step_coords = step_coords[model_idx]
                        # Unpad using the same mask
                        coord_unpad = model_step_coords[pad_mask.bool()]
                        coord_unpad = coord_unpad.cpu().numpy()
                        
                        # Create new structure with intermediate coordinates
                        atoms = structure.atoms.copy()
                        atoms["coords"] = coord_unpad
                        atoms["is_present"] = True
                        
                        new_structure = replace(
                            structure,
                            atoms=atoms,
                            residues=structure.residues,
                            interfaces=np.array([], dtype=Interface)
                        )
                        
                        # Save intermediate structure
                        path = intermediate_dir / f"{record.id}_model_{idx_to_rank[model_idx]}_step_{step}.{self.output_format}"
                        with path.open("w") as f:
                            if self.output_format == "pdb":
                                f.write(to_pdb(new_structure))
                            elif self.output_format == "mmcif":
                                f.write(to_mmcif(new_structure))
                            else:
                                np.savez_compressed(path, **asdict(new_structure))

                # Process final structure
                model_coord = coord[model_idx]
                coord_unpad = model_coord[pad_mask.bool()]
                coord_unpad = coord_unpad.cpu().numpy()

                # New atom table
                atoms = structure.atoms
                atoms["coords"] = coord_unpad
                atoms["is_present"] = True

                # Mew residue table
                residues = structure.residues
                residues["is_present"] = True

                # Update the structure
                interfaces = np.array([], dtype=Interface)
                new_structure: Structure = replace(
                    structure,
                    atoms=atoms,
                    residues=residues,
                    interfaces=interfaces,
                )

                # Update chain info
                chain_info = []
                for chain in new_structure.chains:
                    old_chain_idx = chain_map[chain["asym_id"]]
                    old_chain_info = record.chains[old_chain_idx]
                    new_chain_info = replace(
                        old_chain_info,
                        chain_id=int(chain["asym_id"]),
                        valid=True,
                    )
                    chain_info.append(new_chain_info)

                # Save the structure
                struct_dir = self.output_dir / record.id
                struct_dir.mkdir(exist_ok=True)

                if self.output_format == "pdb":
                    path = (
                        struct_dir / f"{record.id}_model_{idx_to_rank[model_idx]}.pdb"
                    )
                    with path.open("w") as f:
                        f.write(to_pdb(new_structure))
                elif self.output_format == "mmcif":
                    path = (
                        struct_dir / f"{record.id}_model_{idx_to_rank[model_idx]}.cif"
                    )
                    with path.open("w") as f:
                        if "plddt" in prediction:
                            f.write(
                                to_mmcif(new_structure, prediction["plddt"][model_idx])
                            )
                        else:
                            f.write(to_mmcif(new_structure))
                else:
                    path = (
                        struct_dir / f"{record.id}_model_{idx_to_rank[model_idx]}.npz"
                    )
                    np.savez_compressed(path, **asdict(new_structure))

                # Save confidence summary
                if "plddt" in prediction:
                    path = (
                        struct_dir
                        / f"confidence_{record.id}_model_{idx_to_rank[model_idx]}.json"
                    )
                    confidence_summary_dict = {}
                    for key in [
                        "confidence_score",
                        "ptm",
                        "iptm",
                        "ligand_iptm",
                        "protein_iptm",
                        "complex_plddt",
                        "complex_iplddt",
                        "complex_pde",
                        "complex_ipde",
                    ]:
                        confidence_summary_dict[key] = prediction[key][model_idx].item()
                    confidence_summary_dict["chains_ptm"] = {
                        idx: prediction["pair_chains_iptm"][idx][idx][model_idx].item()
                        for idx in prediction["pair_chains_iptm"]
                    }
                    confidence_summary_dict["pair_chains_iptm"] = {
                        idx1: {
                            idx2: prediction["pair_chains_iptm"][idx1][idx2][
                                model_idx
                            ].item()
                            for idx2 in prediction["pair_chains_iptm"][idx1]
                        }
                        for idx1 in prediction["pair_chains_iptm"]
                    }
                    with path.open("w") as f:
                        f.write(
                            json.dumps(
                                confidence_summary_dict,
                                indent=4,
                            )
                        )

                    # Save plddt
                    plddt = prediction["plddt"][model_idx]
                    path = (
                        struct_dir
                        / f"plddt_{record.id}_model_{idx_to_rank[model_idx]}.npz"
                    )
                    np.savez_compressed(path, plddt=plddt.cpu().numpy())

                # Save pae
                if "pae" in prediction:
                    pae = prediction["pae"][model_idx]
                    path = (
                        struct_dir
                        / f"pae_{record.id}_model_{idx_to_rank[model_idx]}.npz"
                    )
                    np.savez_compressed(path, pae=pae.cpu().numpy())

                # Save pde
                if "pde" in prediction:
                    pde = prediction["pde"][model_idx]
                    path = (
                        struct_dir
                        / f"pde_{record.id}_model_{idx_to_rank[model_idx]}.npz"
                    )
                    np.savez_compressed(path, pde=pde.cpu().numpy())

    def on_predict_epoch_end(
        self,
        trainer: Trainer,  # noqa: ARG002
        pl_module: LightningModule,  # noqa: ARG002
    ) -> None:
        """Print the number of failed examples."""
        # Print number of failed examples
        print(f"Number of failed examples: {self.failed}")  # noqa: T201
