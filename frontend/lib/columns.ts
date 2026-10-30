// Canonical import columns. Required ones must map before an import starts.

export interface MappingField {
  name: string;
  label: string;
  required: boolean;
}

export const MAPPING_FIELDS: MappingField[] = [
  { name: "supplier", label: "Supplier", required: true },
  { name: "model", label: "Model / product", required: true },
  { name: "price", label: "Price", required: true },
  { name: "brand", label: "Brand", required: false },
  { name: "capacity", label: "Capacity", required: false },
  { name: "color", label: "Color", required: false },
  { name: "grade", label: "Grade", required: false },
  { name: "lock", label: "Lock state", required: false },
  { name: "quantity", label: "Quantity", required: false },
  { name: "currency", label: "Currency", required: false },
  { name: "conditions", label: "Conditions", required: false },
];
